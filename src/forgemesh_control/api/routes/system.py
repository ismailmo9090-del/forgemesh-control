"""HTTP routes for control-center status, setup, metrics and workers."""

from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from forgemesh_control.agents import AGENT_KINDS, detect_platform
from forgemesh_control.api.deps import get_runtime

router = APIRouter(tags=["system"])


@router.get("/api/status")
async def status(request: Request) -> dict[str, Any]:
    return await get_runtime(request).status_snapshot()


@router.get("/api/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    return get_runtime(request).metrics.snapshot()


@router.get("/api/events/history")
async def event_history(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    since: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    events = await get_runtime(request).events.history(limit=limit, since=since)
    return {"events": events}


@router.get("/api/setup/status")
async def setup_status(request: Request) -> dict[str, Any]:
    return await get_runtime(request).setup_status()


@router.post("/api/setup/start")
async def setup_start(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    started = runtime.start_agent_setup()
    return {
        "started": started,
        "already_running": not started,
        "status": await runtime.setup_status(),
    }


@router.get("/api/setup/requirements")
async def setup_requirements(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    checks = {}
    for name, binary in (
        ("git", "git"),
        ("node", "node"),
        ("npm", "npm"),
        ("bash", "bash"),
        ("curl", "curl"),
        ("pdftotext", "pdftotext"),
        ("tesseract", "tesseract"),
    ):
        found = shutil.which(binary)
        checks[name] = {"found": bool(found), "path": found or ""}
    token_path = runtime.settings.resolved_browser_token_path
    return {
        "platform": detect_platform().to_dict(),
        "checks": checks,
        "browser": {
            "url": runtime.settings.browser_api_url,
            "token_present": token_path.is_file(),
            "token_path": str(token_path),
            "cli": str(runtime.settings.resolved_browser_cli or ""),
        },
        "paths": {
            "data": str(runtime.settings.data_dir),
            "config": str(runtime.settings.config_dir),
            "workspaces": str(runtime.settings.workspaces),
        },
        "agents": list(AGENT_KINDS),
    }


@router.get("/api/browser/status")
async def browser_status(request: Request) -> dict[str, Any]:
    return await get_runtime(request).browser.health_check()


@router.get("/api/browser/profiles")
async def browser_profiles(request: Request) -> dict[str, Any]:
    try:
        return {"profiles": await get_runtime(request).browser.list_profiles()}
    except Exception as exc:  # noqa: BLE001 - the connector reports real failure
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/browser/accounts")
async def browser_accounts(request: Request) -> dict[str, Any]:
    try:
        return {"accounts": await get_runtime(request).browser.accounts()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/logs/{name}")
async def read_log(name: str, request: Request) -> dict[str, Any]:
    if not name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid log name")
    text = await get_runtime(request).log_tail(name)
    return {"name": name, "content": text}


@router.get("/api/config")
async def public_config(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {
        "host": runtime.settings.host,
        "port": runtime.settings.port,
        "data_dir": str(runtime.settings.data_dir),
        "auto_install_agents": runtime.settings.auto_install_agents,
        "council_rounds": runtime.settings.council_rounds,
        "council_max_parallel": runtime.settings.council_max_parallel,
        "agent_timeout_seconds": runtime.settings.agent_timeout_seconds,
    }
