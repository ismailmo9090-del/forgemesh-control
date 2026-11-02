"""Profile management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from forgemesh_browser.api.deps import get_runtime
from forgemesh_browser.core.runtime import Runtime
from forgemesh_browser.profiles.models import ProfileInfo

router = APIRouter(tags=["profiles"])


class ProfileCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str | None = None
    description: str = ""


class ProfileRename(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("", response_model=list[ProfileInfo])
async def list_profiles(runtime: Runtime = Depends(get_runtime)) -> list[ProfileInfo]:
    return await runtime.profiles.list()


@router.post("", response_model=ProfileInfo)
async def create_profile(
    payload: ProfileCreate, runtime: Runtime = Depends(get_runtime)
) -> ProfileInfo:
    return await runtime.profiles.create(
        payload.id, name=payload.name, description=payload.description
    )


@router.get("/{profile_id}")
async def inspect_profile(profile_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    return await runtime.profiles.inspect(profile_id)


@router.post("/{profile_id}/activate", response_model=ProfileInfo)
async def activate_profile(
    profile_id: str, runtime: Runtime = Depends(get_runtime)
) -> ProfileInfo:
    return await runtime.profiles.activate(profile_id)


@router.patch("/{profile_id}", response_model=ProfileInfo)
async def rename_profile(
    profile_id: str, payload: ProfileRename, runtime: Runtime = Depends(get_runtime)
) -> ProfileInfo:
    return await runtime.profiles.rename(profile_id, payload.name)


@router.delete("/{profile_id}")
async def delete_profile(
    profile_id: str,
    confirm: bool = Query(False),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    await runtime.profiles.delete(profile_id, confirm=confirm)
    return {"deleted": profile_id}
