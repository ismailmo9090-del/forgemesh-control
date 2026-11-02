"""Status, events, audit and settings metadata."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from forgemesh_browser import __version__
from forgemesh_browser.api.deps import get_runtime
from forgemesh_browser.core.runtime import Runtime
from forgemesh_browser.providers.registry import ProviderInfo
from forgemesh_browser.storage.models import as_utc
from forgemesh_browser.storage.repositories import audit as audit_repo
from forgemesh_browser.vision.ocr import ocr_available

router = APIRouter(tags=["system"])


@router.get("/status")
async def status(runtime: Runtime = Depends(get_runtime)) -> dict:
    return await runtime.status()


@router.get("/settings")
async def settings_info(runtime: Runtime = Depends(get_runtime)) -> dict:
    settings = runtime.settings
    return {
        "version": __version__,
        "data_dir": str(settings.data_dir),
        "config_dir": str(settings.config_dir),
        "headless_default": settings.headless,
        "browser_channel": settings.browser_channel,
        "viewport": [settings.viewport_width, settings.viewport_height],
        "max_contexts": settings.max_contexts,
        "api": {
            "host": settings.api_host,
            "port": settings.api_port,
            "remote_enabled": settings.api_remote,
        },
        "ai_provider": settings.ai_provider,
        "ollama_host": settings.ollama_host,
        "ollama_model": settings.ollama_model,
        "openai_configured": bool(settings.openai_base_url and settings.openai_api_key),
        "ocr_available": ocr_available(),
    }


@router.get("/events/history")
async def event_history(
    limit: int = 100, runtime: Runtime = Depends(get_runtime)
) -> dict:
    return {"events": runtime.events.history(max(1, min(limit, 500)))}


@router.get("/audit")
async def audit_log(limit: int = 100, runtime: Runtime = Depends(get_runtime)) -> dict:
    async with runtime.db.session_scope() as session:
        rows = await audit_repo.list_recent(session, max(1, min(limit, 500)))
        return {
            "entries": [
                {
                    "id": row.id,
                    "timestamp": as_utc(row.timestamp).isoformat(),
                    "actor": row.actor,
                    "action": row.action,
                    "details": json.loads(row.details_json or "{}"),
                }
                for row in rows
            ]
        }


@router.get("/providers")
async def providers(runtime: Runtime = Depends(get_runtime)) -> dict:
    return {
        "providers": [
            info.model_dump() for info in runtime.providers.list()  # type: ignore[attr-defined]
        ]
    }


@router.get("/providers/{provider_id}")
async def provider(
    provider_id: str, runtime: Runtime = Depends(get_runtime)
) -> ProviderInfo:
    for info in runtime.providers.list():
        if info.id == provider_id:
            return info
    from forgemesh_browser.core.exceptions import AdapterError

    raise AdapterError(f"Unknown provider '{provider_id}'")
