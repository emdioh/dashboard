import hashlib
import re

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import app as app_module
from dashboard import ssh
from dashboard.app import create_app
from dashboard.config import App, Config
from dashboard.proxy import rewrite_location

TRAINS = App(name="trains", url="http://10.0.0.5:5000/", title="Trains")


class Streamed(httpx.AsyncByteStream):
    """A body delivered as a stream, like a real server's (mock bodies are pre-read)."""

    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def streamed(response: httpx.Response) -> httpx.Response:
    return httpx.Response(response.status_code, headers=response.headers.multi_items(),
                          stream=Streamed(response.content))


def upstream(request: httpx.Request) -> httpx.Response:
    return streamed(respond(request))


def respond(request: httpx.Request) -> httpx.Response:
    """Stand-in for a utility running at 10.0.0.5:5000."""
    path = request.url.path
    if path == "/":
        return httpx.Response(200, html="<a href='api/delays'>delays</a>", headers={
            "x-frame-options": "DENY",
            "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
        })
    if path == "/echo":
        return httpx.Response(200, json={
            "method": request.method,
            "query": request.url.query.decode(),
            "body": request.content.decode(),
            "prefix": request.headers.get("x-forwarded-prefix"),
            "host": request.headers.get("host"),
            "forwarded_host": request.headers.get("x-forwarded-host"),
            "forwarded_proto": request.headers.get("x-forwarded-proto"),
            "cookie": request.headers.get("cookie"),
        }, headers=[("set-cookie", "a=1; Path=/"), ("set-cookie", "b=2; Path=/")])
    if path == "/login":
        return httpx.Response(302, headers={"location": "/home"})
    if path == "/abs":
        return httpx.Response(302, headers={"location": "http://10.0.0.5:5000/x?y=1"})
    return httpx.Response(404, text="nope")


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh, "ensure_key", lambda cfg: False)

    def make(handler=upstream):
        cfg = Config(data_dir=tmp_path, hosts={}, services={}, apps={"trains": TRAINS})
        return TestClient(create_app(cfg, http_transport=httpx.MockTransport(handler)))

    return make


def test_apps_page_is_home(make_client):
    with make_client() as client:
        home = client.get("/").text
        assert '<a class="card app-card" href="/apps/trains">' in home and "Trains" in home
        assert 'href="/" aria-current="page">Apps' in home
        assert 'href="/services" >Deploy Services' in home

        page = client.get("/apps/trains").text
        assert '<iframe class="app-frame" src="/proxy/trains/"' in page
        assert '<span class="crumb">Trains</span>' in page
        assert 'aria-current="page">Apps' in page  # Apps stays highlighted inside an app
        assert client.get("/apps/nope").status_code == 404

        services = client.get("/services").text
        assert 'aria-current="page">Deploy Services' in services


def test_stylesheet_url_changes_with_its_content(make_client):
    css = (app_module.HERE / "static/dashboard.css").read_bytes()
    with make_client() as client:
        href = re.search(r'href="(/static/dashboard\.css\?v=\w+)"', client.get("/").text).group(1)
        assert href.endswith(hashlib.sha256(css).hexdigest()[:10])
        assert client.get(href).content == css


def test_proxy_strips_frame_blocking(make_client):
    with make_client() as client:
        r = client.get("/proxy/trains/")
        assert r.status_code == 200 and "api/delays" in r.text
        assert "x-frame-options" not in r.headers
        assert r.headers["content-security-policy"] == "default-src 'self';"


def test_proxy_forwards_request(make_client):
    with make_client() as client:
        client.cookies.set("session", "s1")
        # As cloudflared would send it: the public host, and https.
        r = client.post("/proxy/trains/echo?station=ZH&x=1", content="hello",
                        headers={"host": "dash.example", "x-forwarded-proto": "https"})
        data = r.json()
        assert data == {
            "method": "POST", "query": "station=ZH&x=1", "body": "hello",
            "prefix": "/proxy/trains", "host": "10.0.0.5:5000",
            "forwarded_host": "dash.example", "forwarded_proto": "https",
            "cookie": "session=s1",
        }
        assert r.headers.get_list("set-cookie") == ["a=1; Path=/", "b=2; Path=/"]


def test_proxy_redirects_stay_in_prefix(make_client):
    with make_client() as client:
        assert client.get("/proxy/trains", follow_redirects=False).headers["location"] == "/proxy/trains/"
        assert client.get("/proxy/trains/login", follow_redirects=False).headers["location"] == "/proxy/trains/home"
        assert client.get("/proxy/trains/abs", follow_redirects=False).headers["location"] == "/proxy/trains/x?y=1"


def test_proxy_passes_errors_and_unknown_apps(make_client):
    with make_client() as client:
        assert client.get("/proxy/trains/missing").status_code == 404
        assert client.get("/proxy/nope/").status_code == 404
    with make_client(unreachable) as client:
        r = client.get("/proxy/trains/")
        assert r.status_code == 502 and "not reachable" in r.text


@pytest.mark.parametrize("location, expected", [
    ("/a", "/proxy/trains/a"),
    ("http://10.0.0.5:5000", "/proxy/trains/"),
    ("http://10.0.0.5:5000/b", "/proxy/trains/b"),
    ("https://example.com/c", "https://example.com/c"),
    ("//example.com/d", "//example.com/d"),
    ("relative", "relative"),
])
def test_rewrite_location(location, expected):
    assert rewrite_location(location, TRAINS) == expected
