"""AI provider account management endpoints.

Every action here ends in a real ForgeMesh Browser call. An account with no
verified session is reported as unverified; no route returns a success it did
not observe.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from forgemesh_control.accounts import AccountError
from forgemesh_control.api.deps import get_runtime

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

#: Browser reachability failures are upstream problems, not bad requests.
_UPSTREAM_CODES = {
    "login_begin_failed",
    "login_incomplete",
    "verify_failed",
    "logout_unreachable",
    "logout_unverified",
    "profile_create_failed",
    "profile_delete_failed",
}


class AccountCreate(BaseModel):
    provider_id: str = Field(min_length=1, max_length=40)
    label: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200)
    council_role: str = Field(default="", max_length=60)


class AccountPatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=200)
    council_role: str | None = Field(default=None, max_length=60)
    enabled: bool | None = None


class LoginBegin(BaseModel):
    headless: bool = False


class RemoveBody(BaseModel):
    confirm: bool = False
    purge_profile: bool = True


def _service(request: Request):
    return get_runtime(request).accounts


def _fail(exc: AccountError) -> HTTPException:
    if exc.code == "unknown_account":
        status = 404
    elif exc.code == "confirmation_required" or exc.code == "approval_required":
        status = 409
    elif exc.code in _UPSTREAM_CODES:
        status = 502
    else:
        status = 400
    return HTTPException(status_code=status, detail=exc.message)


@router.get("/providers")
async def providers(request: Request) -> dict[str, Any]:
    return await _service(request).provider_catalog()


@router.get("")
async def summary(request: Request) -> dict[str, Any]:
    return await _service(request).provider_summary()


@router.post("")
async def add(body: AccountCreate, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).add(
            body.provider_id,
            label=body.label,
            email=body.email,
            council_role=body.council_role,
        )
    except AccountError as exc:
        raise _fail(exc) from exc


@router.get("/{account_id}")
async def detail(account_id: str, request: Request) -> dict[str, Any]:
    service = _service(request)
    try:
        account = await service.get(account_id)
    except AccountError as exc:
        raise _fail(exc) from exc
    account["usage"] = await service.usage(account_id)
    return account


@router.patch("/{account_id}")
async def patch(account_id: str, body: AccountPatch, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).patch(
            account_id,
            label=body.label,
            email=body.email,
            council_role=body.council_role,
            enabled=body.enabled,
        )
    except AccountError as exc:
        raise _fail(exc) from exc


@router.post("/{account_id}/active")
async def set_active(account_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).set_active(account_id)
    except AccountError as exc:
        raise _fail(exc) from exc


@router.post("/{account_id}/login/begin")
async def login_begin(account_id: str, request: Request, body: LoginBegin | None = None) -> Any:
    try:
        return await _service(request).begin_login(
            account_id, headless=bool(body and body.headless)
        )
    except AccountError as exc:
        raise _fail(exc) from exc


@router.post("/{account_id}/login/complete")
async def login_complete(account_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).complete_login(account_id)
    except AccountError as exc:
        raise _fail(exc) from exc


@router.post("/{account_id}/verify")
async def verify(account_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).verify(account_id)
    except AccountError as exc:
        raise _fail(exc) from exc


@router.post("/{account_id}/logout")
async def logout(account_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).logout(account_id)
    except AccountError as exc:
        raise _fail(exc) from exc


@router.post("/{account_id}/delete")
async def remove(account_id: str, body: RemoveBody, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).remove(
            account_id, confirm=body.confirm, purge_profile=body.purge_profile
        )
    except AccountError as exc:
        raise _fail(exc) from exc
