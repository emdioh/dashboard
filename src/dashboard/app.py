"""FastAPI web UI. Authentication is expected in front (e.g. Cloudflare Access)."""

import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
from fastapi import FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, proxy, ssh
from .deploy import Deployer

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["ts"] = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def create_app(cfg: config.Config | None = None, http_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.cfg = cfg or config.load()
        ssh.ensure_key(app.state.cfg)
        app.state.deployer = Deployer(app.state.cfg)
        async with httpx.AsyncClient(transport=http_transport, timeout=proxy.TIMEOUT) as client:
            app.state.http = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"apps": request.app.state.cfg.apps, **ctx})

    @app.get("/")
    def index(request: Request):
        st = request.app.state
        return render(request, "index.html", cfg=st.cfg, last=st.deployer.last_per_service())

    @app.post("/services/{name}/deploy")
    async def deploy(request: Request, name: str, ref: str = Form("")):
        st = request.app.state
        if name not in st.cfg.services:
            raise HTTPException(404)
        try:
            job = st.deployer.start(name, ref)
        except (ValueError, RuntimeError) as e:
            return render(request, "_error.html", message=str(e))
        return Response(headers={"HX-Redirect": f"/jobs/{job.id}"})

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: str):
        job = request.app.state.deployer.get(job_id)
        if not job:
            raise HTTPException(404)
        return render(request, "job.html", job=job)

    @app.get("/jobs/{job_id}/log")
    def job_log(request: Request, job_id: str):
        job = request.app.state.deployer.get(job_id)
        if not job:
            raise HTTPException(404)
        return render(request, "_log.html", job=job)

    @app.get("/history")
    def history(request: Request):
        return render(request, "history.html", jobs=request.app.state.deployer.history())

    @app.get("/ssh")
    def ssh_page(request: Request):
        cfg = request.app.state.cfg
        pub = ssh.public_key(cfg)
        commands = {h.name: ssh.authorize_command(pub, h) for h in cfg.hosts.values()}
        return render(request, "ssh.html", cfg=cfg, pubkey=pub, commands=commands)

    @app.post("/hosts/{name}/check")
    async def check_host(request: Request, name: str):
        cfg = request.app.state.cfg
        if name not in cfg.hosts:
            raise HTTPException(404)
        ok, output = await ssh.check(cfg, cfg.hosts[name])
        return render(request, "_check.html", ok=ok, output=output)

    def get_app(request: Request, name: str) -> config.App:
        app_cfg = request.app.state.cfg.apps.get(name)
        if not app_cfg:
            raise HTTPException(404)
        return app_cfg

    @app.get("/apps/{name}")
    def app_page(request: Request, name: str):
        return render(request, "app.html", app=get_app(request, name))

    @app.get("/proxy/{name}")
    def proxy_root(request: Request, name: str):
        # Relative URLs in the app only resolve correctly with the trailing slash.
        return RedirectResponse(f"{proxy.prefix_for(get_app(request, name))}/")

    @app.api_route("/proxy/{name}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy_request(request: Request, name: str, path: str):
        return await proxy.forward(request.app.state.http, get_app(request, name), path, request)

    @app.websocket("/proxy/{name}/{path:path}")
    async def proxy_websocket(websocket: WebSocket, name: str, path: str):
        app_cfg = websocket.app.state.cfg.apps.get(name)
        if not app_cfg:
            await websocket.close(code=1008)
            return
        await proxy.forward_websocket(app_cfg, path, websocket)

    return app
