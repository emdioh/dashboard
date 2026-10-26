"""Reverse proxy that serves each configured app under /proxy/<name>/."""

import re

import httpx2 as httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

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


def request_headers(request: Request, app: App) -> list[tuple[str, str]]:
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
