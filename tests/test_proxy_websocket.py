"""WebSocket proxying, against a real upstream server."""

import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from dashboard import ssh
from dashboard.app import create_app
from dashboard.config import App, Config

closed_by_browser: list[int] = []


async def live(ws: WebSocket):
    """Accepts with the `bearer` subprotocol (like Megachat), reports what it saw, echoes."""
    if "bearer" not in ws.scope["subprotocols"]:
        await ws.close(code=1008)
        return
    await ws.accept(subprotocol="bearer")
    await ws.send_json({
        "subprotocols": ws.scope["subprotocols"],
        "path": ws.url.path,
        "query": ws.url.query,
        "forwarded_host": ws.headers.get("x-forwarded-host"),
        "forwarded_proto": ws.headers.get("x-forwarded-proto"),
        "prefix": ws.headers.get("x-forwarded-prefix"),
        "cookie": ws.headers.get("cookie"),
        "origin": ws.headers.get("origin"),
    })
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                closed_by_browser.append(message["code"])
                return
            if message.get("text") == "close":
                await ws.close(code=4001, reason="bye")
                return
            if message.get("text") is not None:
                await ws.send_text(message["text"])
            else:
                await ws.send_bytes(message["bytes"])
    except WebSocketDisconnect as e:
        closed_by_browser.append(e.code)


@pytest.fixture(scope="module")
def upstream_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        Starlette(routes=[WebSocketRoute("/ws", live)]), host="127.0.0.1", port=port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield port
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def client(tmp_path, monkeypatch, upstream_port):
    monkeypatch.setattr(ssh, "ensure_key", lambda cfg: False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    cfg = Config(data_dir=tmp_path, hosts={}, services={}, apps={
        "live": App(name="live", url=f"http://127.0.0.1:{upstream_port}"),
        "down": App(name="down", url=f"http://127.0.0.1:{closed_port}"),
    })
    with TestClient(create_app(cfg)) as c:
        yield c


def test_relays_messages_and_handshake(client):
    client.cookies.set("session", "s1")
    with client.websocket_connect(
        "/proxy/live/ws?v=2", subprotocols=["bearer", "secret-token"],
        headers={"host": "dash.example", "origin": "https://dash.example", "x-forwarded-proto": "https"},
    ) as ws:
        assert ws.accepted_subprotocol == "bearer"
        assert ws.receive_json() == {
            "subprotocols": ["bearer", "secret-token"],
            "path": "/ws", "query": "v=2",
            "forwarded_host": "dash.example", "forwarded_proto": "https",
            "prefix": "/proxy/live", "cookie": "session=s1", "origin": "https://dash.example",
        }
        ws.send_text("hello")
        assert ws.receive_text() == "hello"
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_bytes() == b"\x00\x01"


def test_app_close_reaches_browser(client):
    with client.websocket_connect("/proxy/live/ws", subprotocols=["bearer"]) as ws:
        ws.receive_json()
        ws.send_text("close")
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4001


def test_browser_close_reaches_app(client):
    closed_by_browser.clear()
    with client.websocket_connect("/proxy/live/ws", subprotocols=["bearer"]) as ws:
        ws.receive_json()
        ws.close(code=4002)
    for _ in range(100):
        if closed_by_browser:
            break
        time.sleep(0.02)
    assert closed_by_browser == [4002]


@pytest.mark.parametrize("path, code", [
    ("/proxy/live/ws", 1008),   # app refuses the handshake (no bearer subprotocol)
    ("/proxy/down/ws", 1011),   # app not reachable
    ("/proxy/nope/ws", 1008),   # unknown app
])
def test_refused_handshakes(client, path, code):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(path):
            pass
    assert e.value.code == code
