"""Identity registry, encrypted vault metadata and prefill login endpoints.

Security notes:

* Passwords are stored encrypted by :class:`forgemesh_browser.security.vault.Vault`
  and are never returned by any endpoint.
* ``POST /identities/{email}/password`` accepts a password over the loopback
  API (protected by the local token) for integrations. The CLI writes the vault
  file directly instead, so normal use never puts a password on the socket.
* Login endpoints only prefill the site's own form. CAPTCHA, OTP and device
  verification stop the flow and return ``needs_human``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from forgemesh_browser.api.deps import get_runtime
from forgemesh_browser.core.runtime import Runtime

router = APIRouter(tags=["identities"])


class IdentityCreate(BaseModel):
    email: str
    label: str = ""
    profile: str | None = None


class PasswordSet(BaseModel):
    password: str
    provider: str | None = None


class IdentityLoginRequest(BaseModel):
    providers: list[str] = Field(default_factory=list)
    headless: bool = False
    timeout_s: int | None = None


class IdentityLoginAllRequest(IdentityLoginRequest):
    emails: list[str] = Field(default_factory=list)


@router.get("/identities")
async def list_identities(runtime: Runtime = Depends(get_runtime)) -> dict:
    identities = await runtime.identities.list()
    return {"identities": [item.model_dump(mode="json") for item in identities]}


@router.post("/identities")
async def create_identity(
    payload: IdentityCreate, runtime: Runtime = Depends(get_runtime)
) -> dict:
    info = await runtime.identities.create(
        payload.email, label=payload.label, profile_id=payload.profile
    )
    return info.model_dump(mode="json")


@router.get("/identities/vault")
async def vault_status(runtime: Runtime = Depends(get_runtime)) -> dict:
    return runtime.vault.status()


@router.delete("/identities/{email}")
async def remove_identity(
    email: str,
    confirm: bool = False,
    delete_profile: bool = False,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    removed = await runtime.identities.remove(
        email, confirm=confirm, delete_profile=delete_profile
    )
    return {"email": email, **removed}


@router.post("/identities/login-all")
async def login_all(
    payload: IdentityLoginAllRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    summary = await runtime.identity_login.login_all(
        emails=payload.emails or None,
        provider_ids=payload.providers or None,
        headless=payload.headless,
        timeout_s=payload.timeout_s,
    )
    return summary.model_dump(mode="json")


@router.post("/identities/{email}/login")
async def login_identity(
    email: str,
    payload: IdentityLoginRequest,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    results = await runtime.identity_login.login(
        email,
        provider_ids=payload.providers or None,
        headless=payload.headless,
        timeout_s=payload.timeout_s,
    )
    return {"results": [item.model_dump(mode="json") for item in results]}


@router.post("/identities/{email}/password")
async def set_password(
    email: str, payload: PasswordSet, runtime: Runtime = Depends(get_runtime)
) -> dict:
    info = await runtime.identities.set_password(
        email, payload.password, provider_id=payload.provider
    )
    return {
        "email": info.email,
        "has_password": info.has_password,
        "password_providers": info.password_providers,
    }


@router.delete("/identities/{email}/password")
async def clear_password(
    email: str,
    provider: str | None = None,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    removed = await runtime.identities.clear_password(email, provider_id=provider)
    return {"email": email, **removed}
