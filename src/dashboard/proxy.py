"""Reverse proxy that serves each configured app under /proxy/<name>/."""

import asyncio
import re

import httpx2 as httpx
from starlette.background import BackgroundTask
from starlette.requests import HTTPConnection, Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from .config import App

# Connection-level headers that must not be forwarded (RFC 9110 7.6.1).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
FRAME_ANCESTORS_RE = re.compile(r"\s*frame-ancestors[^;]*;?", re.IGNORECASE)

# No read timeout, so long polls and server-sent events keep working.
TIMEOUT = httpx.Timeout(30.0, connect=5.0, read=None)


def prefix_for(app: App) -> str:
    return f"/proxy/{app.name}"


def rewrite_location(location: str, app: App) -> str:
    """Keep redirects inside the app's prefix."""
    prefix = prefix_for(app)
    if location == app.url or location.startswith(app.url + "/"):
        return prefix + (location[len(app.url):] or "/")
    if location.startswith("/") and not location.startswith("//"):
        return prefix + location
    return location


def request_headers(request: HTTPConnection, app: App) -> list[tuple[str, str]]:
    headers = [
        (k, v) for k, v in request.headers.items()
        if k not in HOP_BY_HOP and k != "host" and not k.startswith("x-forwarded-")
    ]
    client = request.client.host if request.client else ""
    forwarded_for = ", ".join(filter(None, [request.headers.get("x-forwarded-for"), client]))
    headers += [
        ("x-forwarded-prefix", prefix_for(app)),
        ("x-forwarded-host", request.headers.get("host", "")),
        ("x-forwarded-proto", request.headers.get("x-forwarded-proto", request.url.scheme)),
    ]
    if forwarded_for:
        headers.append(("x-forwarded-for", forwarded_for))
    return headers


def response_headers(resp: httpx.Response, app: App) -> list[tuple[bytes, bytes]]:
    out = []
    for k, v in resp.headers.multi_items():
        k = k.lower()
        if k in HOP_BY_HOP or k == "x-frame-options":
            continue  # the app is shown in the dashboard's frame, so drop frame blocking
        if k == "content-security-policy":
            v = FRAME_ANCESTORS_RE.sub("", v).strip()
            if not v:
                continue
        if k == "location":
            v = rewrite_location(v, app)
        out.append((k.encode("latin-1"), v.encode("latin-1")))
    return out


async def forward(client: httpx.AsyncClient, app: App, path: str, request: Request) -> Response:
    url = f"{app.url}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    upstream = client.build_request(
        request.method, url,
        headers=request_headers(request, app),
        content=request.stream(),
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.RequestError as e:
        return PlainTextResponse(f"{app.title} is not reachable at {app.url}: {e}", status_code=502)

    # Pass the body through untouched (still compressed, if it was).
    response = StreamingResponse(
        resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose),
    )
    response.raw_headers = response_headers(resp, app)
    return response


def close_code(code: int | None) -> int:
    """`code` if it may be sent in a close frame, else a normal closure (1000)."""
    if code is not None and (1000 <= code <= 1014 and code not in (1004, 1005, 1006) or 3000 <= code < 5000):
        return code
    return 1000


async def forward_websocket(app: App, path: str, websocket: WebSocket) -> None:
    """Relay a WebSocket between the browser and the app until either side closes."""
    url = re.sub(r"^http", "ws", app.url) + "/" + path
    if websocket.url.query:
        url += "?" + websocket.url.query
    # The client library sets its own handshake headers (Host, Sec-WebSocket-*).
    headers = [
        (k, v) for k, v in request_headers(websocket, app)
        if not k.startswith("sec-websocket-") and k != "user-agent"
    ]
    try:
        upstream = await ws_connect(
            url,
            subprotocols=websocket.scope.get("subprotocols") or None,
            additional_headers=headers,
            user_agent_header=websocket.headers.get("user-agent"),
            compression=None,
            max_size=None,
            open_timeout=10,
            proxy=None,
        )
    except InvalidStatus as e:
        # Refused by the app (e.g. 401/403): refuse the browser's handshake too.
        await websocket.close(code=1008, reason=f"{app.title} refused: HTTP {e.response.status_code}")
        return
    except (OSError, InvalidHandshake, asyncio.TimeoutError):
        await websocket.close(code=1011, reason=f"{app.title} is not reachable")
        return

    async with upstream:
        await websocket.accept(subprotocol=upstream.subprotocol)

        async def browser_to_app():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(message.get("code", 1000))
                await upstream.send(message["text"] if message.get("text") is not None else message["bytes"])

        async def app_to_browser():
            async for message in upstream:
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)

        tasks = [asyncio.create_task(browser_to_app()), asyncio.create_task(app_to_browser())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        error = next(iter(done)).exception()
        if isinstance(error, WebSocketDisconnect):
            await upstream.close(code=close_code(error.code))
        elif error is None or isinstance(error, ConnectionClosed):
            # The app closed (or dropped) the connection: pass its close code on.
            await websocket.close(code=close_code(upstream.close_code), reason=upstream.close_reason or "")
        else:
            await websocket.close(code=1011)
            raise error
