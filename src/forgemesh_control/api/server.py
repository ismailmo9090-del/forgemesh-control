"""FastAPI application factory for the ForgeMesh Control Center."""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from forgemesh_control import __version__
from forgemesh_control.agents import AgentRegistryError
from forgemesh_control.api import routes
from forgemesh_control.api.deps import require_local, require_token
from forgemesh_control.api.ws import router as ws_router
from forgemesh_control.browser import BrowserUnavailable
from forgemesh_control.config import ControlSettings, _is_loopback, load_control_settings
from forgemesh_control.council import CouncilError
from forgemesh_control.delivery import DeliveryError
from forgemesh_control.documents import DocumentError
from forgemesh_control.projects import ProjectError
from forgemesh_control.runtime import ControlRuntime
from forgemesh_control.scheduler import SchedulerError
from forgemesh_control.security import SecurityError
from forgemesh_control.workspaces import WorkspaceError

PROTECTED = [Depends(require_local), Depends(require_token)]


class SPAStaticFiles(StaticFiles):
    """Serve the React build, falling back to index.html for client routes."""

    async def get_response(self, path: str, scope: dict[str, Any]):  # type: ignore[override]
        if path.startswith("api/") or path in {"auth/session", "auth/logout", "health"}:
            raise StarletteHTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def create_app(settings: ControlSettings | None = None) -> FastAPI:
    resolved = settings or load_control_settings()
    resolved.ensure_dirs()
    runtime = ControlRuntime(resolved)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = resolved
        app.state.runtime = runtime
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="ForgeMesh Control Center",
        version=__version__,
        description=(
            "Central server control plane for ForgeMesh. Loopback-only by default; "
            "mutating routes require the local control token."
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.runtime = runtime

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8787",
            "http://localhost:8787",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(ProjectError)
    async def project_error(_request: Request, exc: ProjectError) -> JSONResponse:
        status = 404 if exc.code == "not_found" else 400
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(AgentRegistryError)
    async def agent_error(_request: Request, exc: AgentRegistryError) -> JSONResponse:
        status = 404 if exc.code == "unknown_agent" else 400
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(DocumentError)
    async def document_error(_request: Request, exc: DocumentError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"error": {"code": "document_error", "message": str(exc)}}
        )

    @app.exception_handler(CouncilError)
    async def council_error(_request: Request, exc: CouncilError) -> JSONResponse:
        status = 404 if exc.code == "not_found" else 400
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(SchedulerError)
    async def scheduler_error(_request: Request, exc: SchedulerError) -> JSONResponse:
        status = 404 if exc.code == "not_found" else 409 if exc.code == "approval_required" else 400
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(WorkspaceError)
    async def workspace_error(_request: Request, exc: WorkspaceError) -> JSONResponse:
        status = 404 if exc.code == "not_a_repo" else 400
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(DeliveryError)
    async def delivery_error(_request: Request, exc: DeliveryError) -> JSONResponse:
        status = 404 if exc.code == "not_found" else 400
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(BrowserUnavailable)
    async def browser_error(_request: Request, exc: BrowserUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "browser_unavailable",
                    "message": exc.message,
                    "details": getattr(exc, "detail", None),
                }
            },
        )

    @app.exception_handler(SecurityError)
    async def security_error(_request: Request, exc: SecurityError) -> JSONResponse:
        return JSONResponse(
            status_code=403, content={"error": {"code": "security_error", "message": str(exc)}}
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "forgemesh-control",
            "version": __version__,
            "pid": os.getpid(),
        }

    @app.get("/auth/session", include_in_schema=False)
    async def auth_session(request: Request, token: str) -> RedirectResponse:
        client_host = request.client.host if request.client else None
        if (
            not resolved.allow_remote
            and client_host not in {"testclient", None}
            and not _is_loopback(client_host or "")
        ):
            raise HTTPException(status_code=403, detail="Loopback access only")
        import hmac

        if not hmac.compare_digest(token, runtime.token):
            raise HTTPException(status_code=401, detail="Invalid control token")
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "fmc_token",
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
            max_age=60 * 60 * 12,
        )
        return response

    @app.get("/auth/logout", include_in_schema=False)
    async def logout() -> RedirectResponse:
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie("fmc_token", path="/")
        return response

    app.include_router(routes.workers.router)
    app.include_router(routes.accounts.router, dependencies=PROTECTED)
    app.include_router(routes.system.router, dependencies=PROTECTED)
    app.include_router(routes.agents.router, dependencies=PROTECTED)
    app.include_router(routes.projects.router, dependencies=PROTECTED)
    app.include_router(routes.documents.router, dependencies=PROTECTED)
    app.include_router(routes.browser.router, dependencies=PROTECTED)
    app.include_router(routes.tasks.router, dependencies=PROTECTED)
    app.include_router(routes.council.router, dependencies=PROTECTED)
    app.include_router(routes.execution.router, dependencies=PROTECTED)
    app.include_router(ws_router)

    dist_dir = Path(resolved.frontend_dist)
    if (dist_dir / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=str(dist_dir), html=True), name="dashboard")
    else:

        @app.get("/", include_in_schema=False)
        async def root() -> HTMLResponse:
            return HTMLResponse(
                "<html><body style='font:14px system-ui;background:#f4f6f9;color:#333c48;"
                "padding:2rem'><h1>ForgeMesh Control Center</h1>"
                "<p>The dashboard build was not found. Run "
                "<code>npm --prefix frontend run build</code>.</p>"
                "<p>API docs: <a style='color:#3b5bdb' href='/docs'>/docs</a></p>"
                "<p>Setup status: <code>/api/setup/status</code> (token required).</p>"
                "</body></html>"
            )

    app.state.started_at = datetime.now(UTC)
    return app


def run() -> None:  # pragma: no cover - exercised by the CLI entry point
    import uvicorn

    settings = load_control_settings()
    uvicorn.run(
        "forgemesh_control.api.server:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
