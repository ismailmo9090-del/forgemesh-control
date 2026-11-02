"""Pydantic models exposed by the profile subsystem."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ProfileInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    active: bool = True
    selected: bool = False
    path: str
    size_bytes: int = 0
    account_count: int = 0
    created_at: datetime
    last_used_at: datetime | None = None


class IsolationReport(BaseModel):
    profile_id: str
    path: str
    within_root: bool
    is_symlink: bool
    mode: str | None = None
    private: bool
    browser_dirs: list[str] = Field(default_factory=list)
    shared_with_other_profiles: list[str] = Field(default_factory=list)
