"""Browser-service connector endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from forgemesh_control.api.deps import get_runtime
from forgemesh_control.browser import BrowserUnavailable

router = APIRouter(prefix="/api/browser", tags=["browser"])


class ProfileBody(BaseModel):
    headless: bool = True


class AskBody(BaseModel):
    provider: str
    prompt: str
    profile: str | None = None
    files: list[str] = Field(default_factory=list)
    timeout_ms: int | None = None


def _connector(request: Request):
    return get_runtime(request).browser


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return await _connector(request).health_check()


@router.get("/providers")
async def providers(request: Request) -> dict[str, Any]:
    try:
        return {"providers": await _connector(request).providers()}
    except BrowserUnavailable as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc


@router.get("/profiles")
async def profiles(request: Request) -> dict[str, Any]:
    try:
        return {"profiles": await _connector(request).list_profiles()}
    except BrowserUnavailable as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc


@router.post("/profiles/{profile_id}/start")
async def start_profile(
    profile_id: str, request: Request, body: ProfileBody | None = None
) -> dict[str, Any]:
    try:
        return await _connector(request).open_profile(
            profile_id, headless=bool(body and body.headless)
        )
    except BrowserUnavailable as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc


@router.post("/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _connector(request).close_profile(profile_id)
    except BrowserUnavailable as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc


@router.get("/accounts")
async def accounts(request: Request, provider: str | None = None) -> dict[str, Any]:
    try:
        return {"accounts": await _connector(request).accounts(provider=provider)}
    except BrowserUnavailable as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc


@router.post("/accounts/{provider}/ask")
async def ask(provider: str, body: AskBody, request: Request) -> dict[str, Any]:
    if body.provider != provider:
        raise HTTPException(status_code=400, detail="Provider in path and body must match")
    try:
        return await _connector(request).ask(
            provider,
            body.prompt,
            profile_id=body.profile,
            files=body.files,
            timeout_ms=body.timeout_ms,
        )
    except BrowserUnavailable as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc


@router.get("/tasks")
async def tasks(request: Request, limit: int = 50) -> dict[str, Any]:
    try:
        return {"tasks": await _connector(request).history(limit=limit)}
    except BrowserUnavailable as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc


@router.get("/events")
async def events(request: Request, limit: int = 50) -> dict[str, Any]:
    try:
        return {"events": await _connector(request).events(limit=limit)}
    except BrowserUnavailable as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
