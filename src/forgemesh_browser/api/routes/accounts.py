"""Account registry, login and provider automation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from forgemesh_browser.api.deps import get_runtime
from forgemesh_browser.core.runtime import Runtime

router = APIRouter(tags=["accounts"])


class LoginBegin(BaseModel):
    profile: str | None = None
    headless: bool = False


class AccountAction(BaseModel):
    profile: str | None = None


class AskRequest(BaseModel):
    profile: str | None = None
    prompt: str = ""
    files: list[str] = Field(default_factory=list)
    timeout_ms: int | None = None


class CaptureRequest(BaseModel):
    profile: str | None = None


@router.get("/accounts")
async def list_accounts(
    profile: str | None = None,
    provider: str | None = None,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    accounts = await runtime.accounts.list_accounts(
        profile_id=profile, provider_id=provider
    )
    return {"accounts": [account.model_dump(mode="json") for account in accounts]}


@router.get("/accounts/status")
async def account_status(
    profile: str | None = None, runtime: Runtime = Depends(get_runtime)
) -> dict:
    summary = await runtime.accounts.summary(profile_id=profile)
    return summary.model_dump(mode="json")


@router.post("/accounts/{provider_id}/login/begin")
async def begin_login(
    provider_id: str, payload: LoginBegin, runtime: Runtime = Depends(get_runtime)
) -> dict:
    profile = payload.profile or runtime.profiles.current()
    return await runtime.accounts.begin_login(
        profile, provider_id, headless=payload.headless
    )


@router.post("/accounts/{provider_id}/login/complete")
async def complete_login(
    provider_id: str, payload: AccountAction, runtime: Runtime = Depends(get_runtime)
) -> dict:
    profile = payload.profile or runtime.profiles.current()
    info = await runtime.accounts.complete_login(profile, provider_id)
    return info.model_dump(mode="json")


@router.get("/accounts/{provider_id}")
async def inspect_account(
    provider_id: str,
    profile: str | None = None,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    profile_id = profile or runtime.profiles.current()
    info = await runtime.accounts.get(profile_id, provider_id)
    return info.model_dump(mode="json")


@router.post("/accounts/{provider_id}/check")
async def check_account(
    provider_id: str, payload: AccountAction, runtime: Runtime = Depends(get_runtime)
) -> dict:
    profile = payload.profile or runtime.profiles.current()
    info = await runtime.accounts.check(profile, provider_id)
    return info.model_dump(mode="json")


@router.post("/accounts/check-all")
async def check_all(
    payload: AccountAction, runtime: Runtime = Depends(get_runtime)
) -> dict:
    accounts = await runtime.accounts.check_all(profile_id=payload.profile)
    return {"accounts": [account.model_dump(mode="json") for account in accounts]}


@router.post("/accounts/{provider_id}/ask")
async def ask(
    provider_id: str, payload: AskRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    profile = payload.profile or runtime.profiles.current()
    await runtime.profiles.require(profile)
    record = await runtime.tasks.execute(
        "account.ask",
        lambda: runtime.accounts.ask(
            profile,
            provider_id,
            payload.prompt,
            files=payload.files,
            timeout_ms=payload.timeout_ms,
        ),
        {"prompt_chars": len(payload.prompt), "files": payload.files},
        profile_id=profile,
        provider_id=provider_id,
    )
    return {**record["result"], "task_id": record["task_id"], "duration_ms": record["duration_ms"]}


@router.post("/accounts/{provider_id}/capture")
async def capture(
    provider_id: str, payload: CaptureRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    profile = payload.profile or runtime.profiles.current()
    return await runtime.accounts.capture_response(profile, provider_id)
