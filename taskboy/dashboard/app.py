"""fastapi application factory for mission control.

serves the json api plus the built react spa. the alb terminates tls and runs auth0
oidc; this app verifies the alb's identity header per request (auth.py). /healthz is
the only unauthenticated data route — alb health checks hit the target directly.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from taskboy.dashboard.api import router
from taskboy.dashboard.auth import NotAuthenticated, NotAuthorized
from taskboy.dashboard.editors import EditorError

logger = logging.getLogger("taskboy.dashboard")


def create_app(store, config, notifier, secrets, orchestrator=None, ui_dist: str = "ui/dist") -> FastAPI:
    app = FastAPI(title="agent dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.config = config
    app.state.notifier = notifier
    app.state.secrets = secrets
    app.state.orchestrator = orchestrator
    app.state.alb_key_cache = {}
    dist = Path(ui_dist)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotAuthenticated)
    async def unauthenticated(request: Request, e: NotAuthenticated):
        return JSONResponse({"detail": str(e)}, status_code=401)

    @app.exception_handler(NotAuthorized)
    async def unauthorized(request: Request, e: NotAuthorized):
        return JSONResponse({"detail": str(e)}, status_code=403)

    @app.exception_handler(EditorError)
    async def editor_missing(request: Request, e: EditorError):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    app.include_router(router)

    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        # client-side routing: any non-api path gets the app shell; real files in dist are served as-is
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        if full_path and "/" not in full_path and ".." not in full_path:
            candidate = dist / full_path
            if candidate.is_file():
                return FileResponse(candidate)
        index = dist / "index.html"
        if not index.is_file():
            return JSONResponse({"detail": "ui build not found — run `npm run build` in ui/"}, status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
