"""Central Agent Registry HTTP API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from forgemesh_control.agent_auth import AgentAuthError
from forgemesh_control.agents import AgentRegistryError
from forgemesh_control.api.deps import get_runtime

router = APIRouter(prefix="/api/agents", tags=["agents"])


class InstallBody(BaseModel):
    force: bool = False


class UpdateBody(BaseModel):
    approve: bool = Field(default=False)


class EnabledBody(BaseModel):
    enabled: bool


class PathBody(BaseModel):
    path: str


def _registry(request: Request):
    return get_runtime(request).agents


@router.get("")
async def list_agents(request: Request) -> dict[str, Any]:
    return {"agents": await _registry(request).list_agents()}


@router.post("/detect")
async def detect_all(request: Request) -> dict[str, Any]:
    return {"agents": await _registry(request).detect_all()}


@router.get("/{kind}")
async def get_agent(kind: str, request: Request) -> dict[str, Any]:
    try:
        return await _registry(request).get_agent(kind)
    except AgentRegistryError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.post("/{kind}/detect")
async def detect(kind: str, request: Request) -> dict[str, Any]:
    try:
        return await _registry(request).detect(kind)
    except AgentRegistryError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.post("/{kind}/verify")
async def verify(kind: str, request: Request) -> dict[str, Any]:
    try:
        return await _registry(request).verify(kind, functional=True)
    except AgentRegistryError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.post("/{kind}/install")
async def install(kind: str, request: Request, body: InstallBody | None = None) -> dict[str, Any]:
    try:
        return await _registry(request).install(
            kind, force=bool(body and body.force), verify=True
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{kind}/reinstall")
async def reinstall(kind: str, request: Request) -> dict[str, Any]:
    try:
        return await _registry(request).reinstall(kind)
    except AgentRegistryError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{kind}/update")
async def update(kind: str, body: UpdateBody, request: Request) -> dict[str, Any]:
    try:
        return await _registry(request).update(kind, approve=body.approve)
    except AgentRegistryError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{kind}/enabled")
async def set_enabled(kind: str, body: EnabledBody, request: Request) -> dict[str, Any]:
    try:
        return await _registry(request).set_enabled(kind, body.enabled)
    except AgentRegistryError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{kind}/path")
async def set_path(kind: str, body: PathBody, request: Request) -> dict[str, Any]:
    try:
        return await _registry(request).set_path(kind, body.path)
    except AgentRegistryError as exc:
        status = 400 if exc.code in {"invalid_path", "wrong_binary"} else 422
        raise HTTPException(status_code=status, detail=exc.message) from exc


@router.get("/{kind}/logs")
async def logs(kind: str, request: Request) -> dict[str, Any]:
    try:
        return await _registry(request).logs(kind)
    except AgentRegistryError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.get("/{kind}/models")
async def models(kind: str, request: Request) -> dict[str, Any]:
    registry = _registry(request)
    try:
        agent = await registry.get_agent(kind)
        if not agent["installed"] or not agent["path"]:
            return {"kind": kind, "models": [], "error": "Agent is not installed."}
        adapter = registry.adapter(kind, agent["path"])
        account = await _auth(request).active_account(kind)
        return {
            "kind": kind,
            "models": await adapter.models_for_account(account),
            "auth_status": agent["auth_status"],
            "account": (account or {}).get("name"),
        }
    except AgentRegistryError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


# ------------------------------------------------------------------ authentication
class LoginBody(BaseModel):
    provider: str = ""
    account_id: int | None = None
    cols: int = Field(default=100, ge=20, le=400)
    rows: int = Field(default=24, ge=5, le=200)


class InputBody(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    session_id: str | None = None


class KeyBody(BaseModel):
    key: str
    session_id: str | None = None


class ResizeBody(BaseModel):
    cols: int = Field(ge=20, le=400)
    rows: int = Field(ge=5, le=200)
    session_id: str | None = None


class SessionRef(BaseModel):
    session_id: str | None = None


class AgentAccountBody(BaseModel):
    name: str


class AgentAccountRemoveBody(BaseModel):
    confirm: bool = False
    purge: bool = False


class ProviderBody(BaseModel):
    provider: str


_AUTH_STATUS = {
    "unknown_session": 404,
    "unknown_account": 404,
    "unknown_agent": 404,
    "not_installed": 409,
    "session_running": 409,
    "session_closed": 409,
    "account_exists": 409,
    "not_signed_in": 409,
    "confirmation_required": 409,
    "invalid_name": 422,
    "invalid_key": 422,
    "empty_input": 422,
    "bad_account": 422,
    "invalid_path": 422,
    "logout_failed": 502,
    "spawn_failed": 502,
    "input_failed": 502,
}


def _auth(request: Request):
    return get_runtime(request).agent_auth


def _auth_error(exc: AgentAuthError) -> HTTPException:
    return HTTPException(
        status_code=_AUTH_STATUS.get(exc.code, 400), detail=exc.message
    )


@router.get("/{kind}/auth")
async def auth_status(kind: str, request: Request) -> dict[str, Any]:
    try:
        return await _auth(request).account_status(kind)
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/auth/login")
async def start_login(kind: str, request: Request, body: LoginBody | None = None) -> dict[str, Any]:
    payload = body or LoginBody()
    try:
        return await _auth(request).start_login(
            kind,
            provider=payload.provider,
            account_id=payload.account_id,
            cols=payload.cols,
            rows=payload.rows,
        )
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.get("/{kind}/auth/session")
async def auth_session(
    kind: str, request: Request, session_id: str | None = None, offset: int = 0
) -> dict[str, Any]:
    try:
        return await _auth(request).session_view(
            kind, session_id, offset=max(0, int(offset))
        )
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/auth/session/input")
async def auth_input(kind: str, body: InputBody, request: Request) -> dict[str, Any]:
    try:
        return await _auth(request).send_text(kind, body.session_id, body.text)
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/auth/session/key")
async def auth_key(kind: str, body: KeyBody, request: Request) -> dict[str, Any]:
    try:
        return await _auth(request).send_key(kind, body.session_id, body.key)
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/auth/session/resize")
async def auth_resize(kind: str, body: ResizeBody, request: Request) -> dict[str, Any]:
    try:
        return await _auth(request).resize(
            kind, body.session_id, cols=body.cols, rows=body.rows
        )
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/auth/session/cancel")
async def auth_cancel(
    kind: str, request: Request, body: SessionRef | None = None
) -> dict[str, Any]:
    try:
        return await _auth(request).cancel(kind, body.session_id if body else None)
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/logout")
async def logout(kind: str, body: ProviderBody, request: Request) -> dict[str, Any]:
    try:
        return await _auth(request).logout(kind, body.provider)
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.get("/{kind}/accounts")
async def agent_accounts(kind: str, request: Request) -> dict[str, Any]:
    auth = _auth(request)
    try:
        accounts = await auth.list_accounts(kind)
        capability = await auth.capability(kind)
        return {
            "kind": kind,
            "accounts": accounts,
            "capability": capability,
            "active_account": await auth.active_account(kind),
        }
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/accounts/sync")
async def sync_agent_accounts(kind: str, request: Request) -> dict[str, Any]:
    try:
        return {"kind": kind, "accounts": await _auth(request).sync_accounts(kind)}
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/accounts")
async def add_agent_account(
    kind: str, body: AgentAccountBody, request: Request
) -> dict[str, Any]:
    try:
        return await _auth(request).add_account(kind, body.name)
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/accounts/{account_id}/active")
async def activate_agent_account(
    kind: str, account_id: int, request: Request
) -> dict[str, Any]:
    try:
        return await _auth(request).set_active(kind, account_id)
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc


@router.post("/{kind}/accounts/{account_id}/delete")
async def delete_agent_account(
    kind: str, account_id: int, request: Request, body: AgentAccountRemoveBody | None = None
) -> dict[str, Any]:
    payload = body or AgentAccountRemoveBody()
    try:
        return await _auth(request).remove_account(
            kind, account_id, confirm=payload.confirm, purge=payload.purge
        )
    except (AgentAuthError, AgentRegistryError) as exc:
        raise _auth_error(exc) from exc
