"""Browser lifecycle, navigation and tabs."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from forgemesh_browser.api.deps import get_runtime
from forgemesh_browser.core.runtime import Runtime

router = APIRouter(tags=["browser"])


def _profile(runtime: Runtime, profile: str | None) -> str:
    return profile or runtime.profiles.current()


class StartRequest(BaseModel):
    profile: str | None = None
    headless: bool | None = None


class NavigateRequest(BaseModel):
    url: str
    profile: str | None = None
    tab_id: str | None = None


class TabRequest(BaseModel):
    profile: str | None = None
    tab_id: str | None = None


class StopRequest(BaseModel):
    profile: str | None = None
    all: bool = False


@router.get("/sessions")
async def sessions(runtime: Runtime = Depends(get_runtime)) -> dict:
    return await runtime.sessions.status()


@router.post("/browser/start")
async def start_browser(
    payload: StartRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    return await runtime.sessions.start(_profile(runtime, payload.profile), headless=payload.headless)


@router.post("/browser/stop")
async def stop_browser(
    payload: StopRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    if payload.all:
        return {"stopped_all": await runtime.sessions.stop_all()}
    profile = _profile(runtime, payload.profile)
    await runtime.sessions.stop(profile)
    return {"stopped": profile}


@router.post("/browser/navigate")
async def navigate(
    payload: NavigateRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    return await runtime.sessions.open(
        _profile(runtime, payload.profile), payload.url, tab_id=payload.tab_id
    )


@router.post("/browser/reload")
async def reload_page(payload: TabRequest, runtime: Runtime = Depends(get_runtime)) -> dict:
    return await runtime.browser.reload(_profile(runtime, payload.profile), tab_id=payload.tab_id)


@router.post("/browser/back")
async def back(payload: TabRequest, runtime: Runtime = Depends(get_runtime)) -> dict:
    return await runtime.browser.go_back(_profile(runtime, payload.profile), tab_id=payload.tab_id)


@router.post("/browser/forward")
async def forward(payload: TabRequest, runtime: Runtime = Depends(get_runtime)) -> dict:
    return await runtime.browser.go_forward(
        _profile(runtime, payload.profile), tab_id=payload.tab_id
    )


@router.get("/browser/tabs")
async def tabs(
    profile: str | None = None, runtime: Runtime = Depends(get_runtime)
) -> dict:
    return {"tabs": await runtime.sessions.tabs(_profile(runtime, profile))}


@router.post("/browser/tabs")
async def new_tab(payload: NavigateRequest, runtime: Runtime = Depends(get_runtime)) -> dict:
    tab = await runtime.browser.new_tab(_profile(runtime, payload.profile), payload.url)
    return tab.model_dump()


@router.post("/browser/tabs/activate")
async def activate_tab(
    payload: TabRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    if not payload.tab_id:
        from forgemesh_browser.core.exceptions import AutomationError

        raise AutomationError("tab_id is required")
    runtime.browser.activate_tab(_profile(runtime, payload.profile), payload.tab_id)
    return {"active_tab": payload.tab_id}


@router.post("/browser/tabs/close")
async def close_tab(payload: TabRequest, runtime: Runtime = Depends(get_runtime)) -> dict:
    await runtime.browser.close_tab(_profile(runtime, payload.profile), payload.tab_id)
    return {"closed": payload.tab_id or "active"}


@router.get("/browser/console")
async def console(
    profile: str | None = None,
    limit: int = 100,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    profile_id = _profile(runtime, profile)
    return {
        "console": runtime.browser.console_logs(profile_id, limit),
        "errors": runtime.browser.errors(profile_id, limit),
    }
