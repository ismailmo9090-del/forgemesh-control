"""Identity registry: one email address, one browser profile, one vault entry.

An *identity* is the human account (``you@example.com``). Every provider
sign-in for that email happens inside its own dedicated profile, because
cookies are per profile: mixing two email addresses in one profile overwrites
the other's sessions.

This module only manages the mapping and the vault entry. The actual prefill
login flow lives in :mod:`forgemesh_browser.accounts.login_runner`.
"""

from __future__ import annotations

import re
from typing import Any

from forgemesh_browser.accounts.models import AccountInfo, IdentityInfo
from forgemesh_browser.accounts.session_health import account_info
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import (
    IdentityError,
    IdentityNotFound,
    ProfileError,
    ProfileNotFound,
)
from forgemesh_browser.profiles.manager import ProfileManager
from forgemesh_browser.providers.registry import ProviderRegistry
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.security.vault import Vault, normalize_email
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database
from forgemesh_browser.storage.models import IdentityRow, as_utc
from forgemesh_browser.storage.repositories import accounts as account_repo
from forgemesh_browser.storage.repositories import identities as identity_repo

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PROFILE_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not EMAIL_RE.match(normalized):
        raise IdentityError(f"Not a valid email address: {email!r}")
    if len(normalized) > 320:
        raise IdentityError("Email address is too long (max 320 characters).")
    return normalized


def profile_slug(email: str) -> str:
    """Derive a stable profile id from the email's local part."""

    local = email.split("@", 1)[0].lower()
    slug = _PROFILE_SLUG_RE.sub("-", local).strip("-_")
    slug = slug[:40].strip("-_")
    return slug or "identity"


class IdentityManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        profiles: ProfileManager,
        providers: ProviderRegistry,
        vault: Vault,
        events: EventBus,
        audit: AuditLogger,
    ) -> None:
        self.settings = settings
        self.db = db
        self.profiles = profiles
        self.providers = providers
        self.vault = vault
        self.events = events
        self.audit = audit

    # ------------------------------------------------------------------ helpers
    def _info(
        self,
        row: IdentityRow,
        accounts: list[AccountInfo] | None = None,
        secrets: dict[str, dict[str, Any]] | None = None,
    ) -> IdentityInfo:
        if secrets is None:
            secrets = self.vault.index()
        entry = secrets.get(row.email, {})
        return IdentityInfo(
            email=row.email,
            label=row.label or "",
            profile_id=row.profile_id,
            has_password=bool(entry.get("has_password")),
            password_providers=list(entry.get("providers") or []),
            created_at=as_utc(row.created_at),
            last_login_at=as_utc(row.last_login_at),
            accounts=accounts or [],
        )

    def _accounts_for(
        self, rows: list[tuple[str, AccountInfo]], profile_id: str
    ) -> list[AccountInfo]:
        return [info for owner, info in rows if owner == profile_id]

    def provider_name(self, provider_id: str) -> str:
        return self.providers.name(provider_id)

    async def _account_index(self) -> list[tuple[str, AccountInfo]]:
        async with self.db.session_scope() as session:
            rows = await account_repo.list_all(session)
        index: list[tuple[str, AccountInfo]] = []
        for row in rows:
            index.append(
                (
                    row.profile_id,
                    account_info(row, self.provider_name(row.provider_id)),
                )
            )
        return index

    # -------------------------------------------------------------------- CRUD
    async def create(
        self,
        email: str,
        *,
        label: str = "",
        profile_id: str | None = None,
    ) -> IdentityInfo:
        email = validate_email(email)
        target = profile_id or profile_slug(email)
        if target != profile_slug(email):
            target = target.strip().lower()

        async with self.db.session_scope() as session:
            existing = await identity_repo.get(session, email)
            if existing is not None:
                raise IdentityError(
                    f"Identity already exists: {email} (profile '{existing.profile_id}')"
                )
            bound = await identity_repo.get_by_profile(session, target)
            if bound is not None and bound.email != email:
                raise IdentityError(
                    f"Profile '{target}' already belongs to {bound.email}. "
                    "Pick another profile id with --profile-id."
                )

        profile_info = None
        try:
            profile_info = await self.profiles.get(target)
        except ProfileNotFound:
            profile_info = None
        except ProfileError as exc:
            raise IdentityError(str(exc)) from exc

        if profile_info is None:
            profile_info = await self.profiles.create(
                target, name=label or email, description=f"Identity {email}"
            )

        async with self.db.session_scope() as session:
            row = await identity_repo.create(
                session, email, profile_info.id, label=label
            )
            info = self._info(row)

        await self.audit.record(
            "identity.create",
            details={"email": email, "profile": profile_info.id},
        )
        self.events.publish(
            "identity.created", email=email, profile=profile_info.id
        )
        return info

    async def list(self) -> list[IdentityInfo]:
        accounts = await self._account_index()
        secrets = self.vault.index()
        async with self.db.session_scope() as session:
            rows = await identity_repo.list_all(session)
            return [
                self._info(row, self._accounts_for(accounts, row.profile_id), secrets)
                for row in rows
            ]

    async def get(self, email: str) -> IdentityInfo:
        email = validate_email(email)
        accounts = await self._account_index()
        async with self.db.session_scope() as session:
            row = await identity_repo.get(session, email)
            if row is None:
                raise IdentityNotFound(
                    f"Unknown identity: {email}. Run: fmb identity add {email}"
                )
            return self._info(row, self._accounts_for(accounts, row.profile_id))

    async def require_row(self, email: str) -> IdentityRow:
        email = validate_email(email)
        async with self.db.session_scope() as session:
            row = await identity_repo.get(session, email)
            if row is None:
                raise IdentityNotFound(
                    f"Unknown identity: {email}. Run: fmb identity add {email}"
                )
            return row

    async def remove(
        self, email: str, *, confirm: bool = False, delete_profile: bool = False
    ) -> dict[str, bool]:
        if not confirm:
            raise IdentityError("Identity removal requires explicit confirmation")
        email = validate_email(email)
        row = await self.require_row(email)
        profile_id = row.profile_id
        if self.profiles.is_running(profile_id):
            raise ProfileError(
                f"Profile '{profile_id}' is running. Stop it first: "
                f"fmb stop --profile {profile_id}"
            )
        async with self.db.session_scope() as session:
            await identity_repo.remove(session, email)
        removed_password = self.vault.delete_password(email)
        if delete_profile:
            try:
                await self.profiles.delete(profile_id, confirm=True)
            except (ProfileError, ProfileNotFound):
                delete_profile = False
        await self.audit.record(
            "identity.remove",
            details={
                "email": email,
                "profile": profile_id,
                "password_removed": removed_password,
                "profile_deleted": delete_profile,
            },
        )
        self.events.publish("identity.removed", email=email, profile=profile_id)
        return {"password_removed": removed_password, "profile_deleted": delete_profile}

    # ---------------------------------------------------------------- passwords
    async def set_password(
        self, email: str, password: str, *, provider_id: str | None = None
    ) -> IdentityInfo:
        await self.require_row(email)
        self.vault.set_password(email, password, provider=provider_id)
        await self.audit.record(
            "identity.password_set",
            details={"email": normalize_email(email), "provider": provider_id or "*"},
        )
        return await self.get(email)

    async def clear_password(
        self, email: str, *, provider_id: str | None = None
    ) -> dict[str, bool]:
        await self.require_row(email)
        removed = self.vault.delete_password(email, provider=provider_id)
        await self.audit.record(
            "identity.password_cleared",
            details={"email": normalize_email(email), "provider": provider_id or "*"},
        )
        return {"removed": removed}

    async def touch_login(self, email: str) -> None:
        async with self.db.session_scope() as session:
            await identity_repo.touch_login(session, validate_email(email))
