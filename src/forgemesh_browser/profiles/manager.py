"""Chrome-like isolated profile management."""

from __future__ import annotations

import shutil
from collections.abc import Callable

from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import ProfileError, ProfileInUse, ProfileNotFound
from forgemesh_browser.profiles.isolation import inspect_isolation
from forgemesh_browser.profiles.models import ProfileInfo
from forgemesh_browser.profiles.storage import (
    AppState,
    directory_size,
    ensure_profile_layout,
    profile_path,
    read_state,
    selected_profile,
    validate_profile_id,
    write_state,
)
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database
from forgemesh_browser.storage.models import as_utc
from forgemesh_browser.storage.repositories import accounts as account_repo
from forgemesh_browser.storage.repositories import identities as identity_repo
from forgemesh_browser.storage.repositories import profiles as profile_repo


class ProfileManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        events: EventBus,
        audit: AuditLogger,
        is_running: Callable[[str], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.audit = audit
        self.is_running = is_running or (lambda _profile_id: False)

    def path(self, profile_id: str):
        return profile_path(self.settings, profile_id)

    def _to_info(self, row, selected: str, size: int, account_count: int) -> ProfileInfo:
        return ProfileInfo(
            id=row.id,
            name=row.name,
            description=row.description or "",
            active=row.active,
            selected=row.id == selected,
            path=str(profile_path(self.settings, row.id)),
            size_bytes=size,
            account_count=account_count,
            created_at=as_utc(row.created_at),
            last_used_at=as_utc(row.last_used_at),
        )

    async def create(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        description: str = "",
    ) -> ProfileInfo:
        profile_id = validate_profile_id(profile_id)
        async with self.db.session_scope() as session:
            if await profile_repo.get(session, profile_id) is not None:
                raise ProfileError(f"Profile already exists: {profile_id}")
            ensure_profile_layout(self.settings, profile_id)
            row = await profile_repo.create(
                session, profile_id, name or profile_id.replace("-", " ").title(), description
            )
            info = self._to_info(row, selected_profile(self.settings, ""), 0, 0)

        if read_state(self.settings).active_profile is None:
            write_state(self.settings, AppState(active_profile=profile_id))
            info.selected = True

        await self.audit.record("profile.create", details={"profile": profile_id})
        self.events.publish("profile.created", profile=info.model_dump(mode="json"))
        return info

    async def ensure_default(self) -> ProfileInfo | None:
        async with self.db.session_scope() as session:
            rows = await profile_repo.list_all(session)
            if rows:
                state = read_state(self.settings)
                if state.active_profile is None or all(
                    row.id != state.active_profile for row in rows
                ):
                    write_state(self.settings, AppState(active_profile=rows[0].id))
                return None
        return await self.create(self.settings.default_profile, name="Primary")

    async def list(self) -> list[ProfileInfo]:
        selected = selected_profile(self.settings, self.settings.default_profile)
        async with self.db.session_scope() as session:
            rows = await profile_repo.list_all(session)
            infos = []
            for row in rows:
                count = await profile_repo.count_accounts(session, row.id)
                infos.append(
                    self._to_info(
                        row,
                        selected,
                        directory_size(profile_path(self.settings, row.id)),
                        count,
                    )
                )
        return infos

    async def get(self, profile_id: str) -> ProfileInfo:
        profile_id = validate_profile_id(profile_id)
        selected = selected_profile(self.settings, self.settings.default_profile)
        async with self.db.session_scope() as session:
            row = await profile_repo.get(session, profile_id)
            if row is None:
                raise ProfileNotFound(f"Profile not found: {profile_id}")
            count = await profile_repo.count_accounts(session, profile_id)
            return self._to_info(
                row,
                selected,
                directory_size(profile_path(self.settings, profile_id)),
                count,
            )

    async def require(self, profile_id: str) -> ProfileInfo:
        return await self.get(profile_id)

    def current(self) -> str:
        return selected_profile(self.settings, self.settings.default_profile)

    async def inspect(self, profile_id: str) -> dict:
        info = await self.get(profile_id)
        isolation = inspect_isolation(self.settings, profile_id)
        async with self.db.session_scope() as session:
            rows = await account_repo.list_all(session, profile_id=profile_id)
            accounts = [
                {
                    "provider_id": row.provider_id,
                    "status": row.status,
                    "last_ok_at": as_utc(row.last_ok_at).isoformat()
                    if row.last_ok_at
                    else None,
                    "last_error": row.last_error,
                }
                for row in rows
            ]
        return {
            "profile": info.model_dump(mode="json"),
            "isolation": isolation.model_dump(mode="json"),
            "accounts": accounts,
            "running": self.is_running(profile_id),
        }

    async def rename(self, profile_id: str, new_name: str) -> ProfileInfo:
        new_name = new_name.strip()
        if not new_name:
            raise ProfileError("Profile name cannot be empty")
        await self.get(profile_id)
        async with self.db.session_scope() as session:
            await profile_repo.rename(session, profile_id, new_name)
        await self.audit.record(
            "profile.rename", details={"profile": profile_id, "name": new_name}
        )
        return await self.get(profile_id)

    async def activate(self, profile_id: str) -> ProfileInfo:
        info = await self.get(profile_id)
        write_state(self.settings, AppState(active_profile=profile_id))
        await self.audit.record("profile.activate", details={"profile": profile_id})
        self.events.publish("profile.activated", profile=profile_id)
        info.selected = True
        return info

    async def delete(self, profile_id: str, *, confirm: bool = False) -> None:
        if not confirm:
            raise ProfileError("Profile deletion requires explicit confirmation")
        await self.get(profile_id)
        if self.is_running(profile_id):
            raise ProfileInUse(
                f"Profile '{profile_id}' is running. Stop it first: fmb stop --profile {profile_id}"
            )
        async with self.db.session_scope() as session:
            bound = await identity_repo.get_by_profile(session, profile_id)
            if bound is not None:
                raise ProfileError(
                    f"Profile '{profile_id}' belongs to identity {bound.email}. "
                    f"Remove the identity first: fmb identity remove {bound.email}"
                )
            if not await profile_repo.delete(session, profile_id):
                raise ProfileNotFound(f"Profile not found: {profile_id}")

        path = profile_path(self.settings, profile_id)
        if path.exists():
            shutil.rmtree(path)

        state = read_state(self.settings)
        if state.active_profile == profile_id:
            remaining = await self.list()
            write_state(
                self.settings,
                AppState(active_profile=remaining[0].id if remaining else None),
            )

        await self.audit.record(
            "profile.delete", details={"profile": profile_id, "path": str(path)}
        )
        self.events.publish("profile.deleted", profile=profile_id)

    async def touch(self, profile_id: str) -> None:
        async with self.db.session_scope() as session:
            await profile_repo.touch(session, profile_id)
