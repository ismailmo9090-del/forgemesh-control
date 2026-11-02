"""FastAPI dependencies for the control server."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from forgemesh_control.config import _is_loopback
from forgemesh_control.runtime import ControlRuntime


def get_runtime(request: Request) -> ControlRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - lifespan guarantees this
        raise HTTPException(status_code=503, detail="Control runtime is not ready")
    return runtime


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("x-api-token")
    if header:
        return header
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    cookie = request.cookies.get("fmc_token")
    if cookie:
        return cookie
    return request.query_params.get("token")


async def require_token(request: Request) -> None:
    runtime = get_runtime(request)
    provided = _extract_token(request)
    if not provided or not hmac.compare_digest(provided, runtime.token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing control token (X-API-Token header or session cookie)",
        )


async def require_local(request: Request) -> None:
    runtime = get_runtime(request)
    if runtime.settings.allow_remote:
        return
    client_host = request.client.host if request.client else None
    if client_host in {"testclient", None}:
        return
    if not _is_loopback(client_host or ""):
        raise HTTPException(
            status_code=403,
            detail="Remote access is disabled. Set FMC_ALLOW_REMOTE=true only behind TLS.",
        )
