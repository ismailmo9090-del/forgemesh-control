"""FastAPI application factory for the local ForgeMesh Browser daemon."""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from forgemesh_browser import __version__
from forgemesh_browser.api.deps import require_local, require_token
from forgemesh_browser.api.routes import (
    accounts,
    ai,
    automation,
    browser,
    identities,
    media,
    profiles,
    system,
    tasks,
)
from forgemesh_browser.api.websocket import router as websocket_router
from forgemesh_browser.core.exceptions import ForgeMeshError
from forgemesh_browser.core.runtime import Runtime
from forgemesh_browser.security.permissions import secure_file
from forgemesh_browser.settings import Settings, load_settings

PROTECTED = [Depends(require_local), Depends(require_token)]


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = Runtime(resolved)
        await runtime.start()
        app.state.runtime = runtime
        info = {
            "pid": os.getpid(),
            "host": resolved.api_host,
            "port": resolved.api_port,
            "started_at": datetime.now(UTC).isoformat(),
            "version": __version__,
        }
        try:
            resolved.daemon_info_path.write_text(json.dumps(info), encoding="utf-8")
            secure_file(resolved.daemon_info_path)
        except OSError:
            pass
        try:
            yield
        finally:
            await runtime.stop()
            try:
                if resolved.daemon_info_path.is_file():
                    current = json.loads(resolved.daemon_info_path.read_text(encoding="utf-8"))
                    if current.get("pid") == os.getpid():
                        resolved.daemon_info_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

    app = FastAPI(
        title="ForgeMesh Browser API",
        version=__version__,
        description=(
            "Local API for the terminal-first ForgeMesh browser runtime. "
            "Requires X-API-Token header. Bound to localhost by default."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(ForgeMeshError)
    async def forge_error(_request: Request, exc: ForgeMeshError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok", "service": "forgemesh-browser", "version": __version__}

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "service": "forgemesh-browser",
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
            "authentication": "X-API-Token header (stored in data/runtime/api_token)",
        }

    app.include_router(system.router, dependencies=PROTECTED)
    app.include_router(profiles.router, prefix="/profiles", dependencies=PROTECTED)
    app.include_router(browser.router, dependencies=PROTECTED)
    app.include_router(automation.router, dependencies=PROTECTED)
    app.include_router(accounts.router, dependencies=PROTECTED)
    app.include_router(identities.router, dependencies=PROTECTED)
    app.include_router(media.router, dependencies=PROTECTED)
    app.include_router(ai.router, dependencies=PROTECTED)
    app.include_router(tasks.router, dependencies=PROTECTED)
    app.include_router(websocket_router)
    return app
