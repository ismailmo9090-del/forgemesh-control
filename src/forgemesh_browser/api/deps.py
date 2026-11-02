"""FastAPI dependencies: runtime access, token auth and localhost enforcement."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from forgemesh_browser.core.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - lifespan guarantees this
        raise HTTPException(status_code=503, detail="Runtime is not ready")
    return runtime


def _extract_token(request: Request) -> str | None:
    token = request.headers.get("x-api-token")
    if token:
        return token
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def require_token(request: Request) -> None:
    runtime = get_runtime(request)
    provided = _extract_token(request)
    if not provided or not hmac.compare_digest(provided, runtime.api_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API token (set X-API-Token header)",
        )


def is_local_host(host: str | None) -> bool:
    return host in {"127.0.0.1", "::1", "localhost", "testclient", None}


def require_local(request: Request) -> None:
    runtime = get_runtime(request)
    if runtime.settings.api_remote:
        return
    client_host = request.client.host if request.client else None
    if not is_local_host(client_host):
        raise HTTPException(
            status_code=403,
            detail="Remote API access is disabled. Set FMB_API_REMOTE=true and use a token.",
        )
