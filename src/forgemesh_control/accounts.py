"""Multiple accounts per AI provider, each with its own browser profile.

Policy: a ForgeMesh account is a *label plus an isolated ForgeMesh Browser
profile*. The cookie jar and local storage stay inside the browser runtime's
profile directory; this service never sees, stores or returns them. Login is
always performed by the human in a visible window - nothing here types a
password, solves a challenge, or rotates an account to dodge a limit. When the
browser runtime cannot confirm a session, the account is reported as unverified
rather than as logged in.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select

from forgemesh_control.audit import AuditService
from forgemesh_control.browser import BrowserConnector, BrowserUnavailable
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.models import AiAccountRow, MeetingRow, ParticipantRow, iso, utcnow
from forgemesh_control.storage import ControlDatabase

logger = logging.getLogger("forgemesh.control.accounts")

#: Providers the council knows about, in display order. Any other provider the
#: browser runtime exposes is still manageable - this only fixes the ordering.
PREFERRED_PROVIDERS = ("chatgpt", "gemini", "claude", "deepseek", "perplexity", "sarvam")

#: Mirrors the browser runtime's own vocabulary so a status means the same
#: thing in both services.
STATUS_VERIFIED = "available"
STATUS_REAUTH = "needs_reauth"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_ERROR = "error"
STATUS_PENDING = "login_pending"
STATUS_UNVERIFIED = "unverified"

PROFILE_ID_MAX = 64
_UNSAFE_PROFILE_CHARS = re.compile(r"[^a-z0-9_-]+")
_VALID_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AccountError(RuntimeError):
    def __init__(self, message: str, *, code: str = "account_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _slug(value: str) -> str:
    cleaned = _UNSAFE_PROFILE_CHARS.sub("-", value.strip().lower()).strip("-_")
    return cleaned or "account"


def _row_dict(row: AiAccountRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "label": row.label,
        "email": row.email,
        "profile_id": row.profile_id,
        "council_role": row.council_role,
        "active": bool(row.active),
        "enabled": bool(row.enabled),
        "status": row.status,
        "verified": row.status == STATUS_VERIFIED,
        "last_login_at": iso(row.last_login_at),
        "last_verified_at": iso(row.last_verified_at),
        "last_error": row.last_error,
        "last_error_at": iso(row.last_error_at),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


class AiAccountService:
    def __init__(
        self,
        settings: ControlSettings,
        db: ControlDatabase,
        events: EventStore,
        audit: AuditService,
        browser: BrowserConnector,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.audit = audit
        self.browser = browser

    # ------------------------------------------------------------------ providers
    async def provider_catalog(self) -> dict[str, Any]:
        """The provider list the browser runtime actually serves."""
        try:
            providers = await self.browser.providers()
        except BrowserUnavailable as exc:
            return {
                "connected": False,
                "error": exc.message,
                "providers": [
                    {
                        "id": provider_id,
                        "name": provider_id.title(),
                        "url": "",
                        "enabled": True,
                        "login_supported": True,
                        "managed_by_browser": False,
                    }
                    for provider_id in PREFERRED_PROVIDERS
                ],
            }
        known = {str(item.get("id")) for item in providers}
        ordered = [
            {**item, "managed_by_browser": True, "council": item.get("id") in PREFERRED_PROVIDERS}
            for item in providers
        ]
        for provider_id in PREFERRED_PROVIDERS:
            if provider_id not in known:
                ordered.append(
                    {
                        "id": provider_id,
                        "name": provider_id.title(),
                        "url": "",
                        "enabled": False,
                        "login_supported": False,
                        "managed_by_browser": False,
                        "council": True,
                    }
                )
        ordered.sort(key=lambda item: (item.get("id") not in PREFERRED_PROVIDERS, str(item)))
        return {"connected": True, "error": None, "providers": ordered}

    async def _require_provider(self, provider_id: str) -> dict[str, Any]:
        catalog = await self.provider_catalog()
        for item in catalog["providers"]:
            if item["id"] == provider_id:
                if not item.get("managed_by_browser"):
                    raise AccountError(
                        f"{provider_id} is not driven by the ForgeMesh Browser runtime, "
                        "so it has no login page to open here.",
                        code="unsupported_provider",
                    )
                return item
        raise AccountError(f"Unknown provider '{provider_id}'.", code="unknown_provider")

    # ------------------------------------------------------------------ accounts
    async def _profile_taken(self, profile_id: str) -> bool:
        async with self.db.session_scope() as session:
            found = (
                await session.execute(
                    select(AiAccountRow).where(AiAccountRow.profile_id == profile_id)
                )
            ).scalars().first()
        return found is not None

    async def _new_profile_id(self, provider_id: str, label: str) -> str:
        base = f"{_slug(provider_id)}-{_slug(label)}"[:PROFILE_ID_MAX]
        candidate = base
        suffix = 2
        while True:
            if candidate != "primary" and _VALID_PROFILE_ID.match(candidate):
                try:
                    existing = await self.browser.list_profiles()
                except BrowserUnavailable:
                    existing = []
                taken = {str(item.get("id")) for item in existing}
                if candidate not in taken and not await self._profile_taken(candidate):
                    return candidate
            candidate = f"{base[:-3]}-{suffix}"[:PROFILE_ID_MAX]
            suffix += 1
            if suffix > 50:
                raise AccountError(
                    f"Could not allocate a free browser profile for {label}.",
                    code="profile_exhausted",
                )

    async def add(
        self,
        provider_id: str,
        *,
        label: str = "",
        email: str = "",
        council_role: str = "",
    ) -> dict[str, Any]:
        provider = await self._require_provider(provider_id)
        clean_label = (label or "").strip()[:120]
        if not clean_label:
            clean_label = await self._next_label(provider_id)
        async with self.db.session_scope() as session:
            clash = (
                await session.execute(
                    select(AiAccountRow).where(
                        AiAccountRow.provider_id == provider_id,
                        AiAccountRow.label == clean_label,
                    )
                )
            ).scalars().first()
        if clash is not None:
            raise AccountError(
                f"{provider['name']} already has an account called '{clean_label}'.",
                code="duplicate_label",
            )
        clean_email = (email or "").strip()[:200]
        if any(char in clean_email for char in (" ", "\n", "\t")) and clean_email:
            if "@" not in clean_email:
                raise AccountError(
                    "The email field is optional identification. Put one address in it, "
                    "or leave it empty - never a password.",
                    code="invalid_email",
                )
        profile_id = await self._new_profile_id(provider_id, clean_label)
        try:
            profile = await self.browser.create_profile(
                profile_id,
                name=f"{provider['name']} - {clean_label}",
                description=f"ForgeMesh account {clean_label} for {provider_id}",
            )
        except BrowserUnavailable as exc:
            raise AccountError(
                f"The browser runtime refused to create profile {profile_id}: {exc.message}",
                code="profile_create_failed",
            ) from exc

        account_id = f"acct_{uuid.uuid4().hex[:10]}"
        async with self.db.session_scope() as session:
            row = AiAccountRow(
                id=account_id,
                provider_id=provider_id,
                label=clean_label,
                email=clean_email,
                profile_id=profile_id,
                council_role=(council_role or "").strip()[:60],
                active=not await self._has_active(session, provider_id),
                status=STATUS_UNVERIFIED,
            )
            session.add(row)
            await session.flush()
            created = _row_dict(row)
        created["profile"] = profile
        await self.audit.record(
            "account.added",
            actor="dashboard",
            account_id=account_id,
            provider=provider_id,
            profile=profile_id,
        )
        await self.events.publish(
            "account.added",
            account_id=account_id,
            provider_id=provider_id,
            label=clean_label,
            important=True,
        )
        return created

    async def _next_label(self, provider_id: str) -> str:
        existing = await self.list(provider_id=provider_id)
        index = len(existing) + 1
        used = {str(item["label"]).lower() for item in existing}
        while f"Account {index}".lower() in used:
            index += 1
        return f"Account {index}"

    async def _has_active(self, session, provider_id: str) -> bool:  # noqa: ANN001
        rows = (
            (
                await session.execute(
                    select(AiAccountRow).where(
                        AiAccountRow.provider_id == provider_id,
                        AiAccountRow.active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        return bool(rows)

    async def list(self, *, provider_id: str | None = None) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            query = select(AiAccountRow).order_by(AiAccountRow.provider_id, AiAccountRow.label)
            if provider_id:
                query = query.where(AiAccountRow.provider_id == provider_id)
            rows = (await session.execute(query)).scalars().all()
        return [_row_dict(row) for row in rows]

    async def get_row(self, account_id: str) -> AiAccountRow:
        async with self.db.session_scope() as session:
            row = await session.get(AiAccountRow, account_id)
        if row is None:
            raise AccountError(f"No account '{account_id}'.", code="unknown_account")
        return row

    async def get(self, account_id: str) -> dict[str, Any]:
        return _row_dict(await self.get_row(account_id))

    async def patch(
        self,
        account_id: str,
        *,
        label: str | None = None,
        email: str | None = None,
        council_role: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await session.get(AiAccountRow, account_id)
            if row is None:
                raise AccountError(f"No account '{account_id}'.", code="unknown_account")
            if label is not None and label.strip():
                row.label = label.strip()[:120]
                try:
                    await self.browser.rename_profile(row.profile_id, f"{row.provider_id} - {row.label}")
                except BrowserUnavailable:
                    # The registry rename still stands; the browser keeps its own
                    # profile name and the caller is told which part failed.
                    row.last_error = "Browser profile name was not updated."
            if email is not None:
                clean = email.strip()[:200]
                if clean and " " in clean:
                    raise AccountError("An email address cannot contain spaces.", code="invalid_email")
                row.email = clean
            if council_role is not None:
                row.council_role = council_role.strip()[:60]
            if enabled is not None:
                row.enabled = bool(enabled)
            row.updated_at = utcnow()
            stored = _row_dict(row)
        await self.audit.record(
            "account.updated",
            account_id=account_id,
            label=stored["label"],
            council_role=stored["council_role"],
            enabled=stored["enabled"],
            email_set=bool(stored["email"]),
        )
        await self.events.publish("account.updated", account_id=account_id, important=False)
        return stored

    async def set_active(self, account_id: str) -> dict[str, Any]:
        row = await self.get_row(account_id)
        async with self.db.session_scope() as session:
            others = (
                (
                    await session.execute(
                        select(AiAccountRow).where(
                            AiAccountRow.provider_id == row.provider_id,
                            AiAccountRow.id != account_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for other in others:
                other.active = False
            target = await session.get(AiAccountRow, account_id)
            assert target is not None
            target.active = True
            target.enabled = True
            result = _row_dict(target)
        await self.audit.record(
            "account.activated", account_id=account_id, provider=row.provider_id,
            profile=row.profile_id,
        )
        await self.events.publish(
            "account.activated",
            account_id=account_id,
            provider_id=row.provider_id,
            profile_id=row.profile_id,
            important=True,
        )
        return result

    # --------------------------------------------------------------------- login
    async def begin_login(self, account_id: str, *, headless: bool = False) -> dict[str, Any]:
        row = await self.get_row(account_id)
        try:
            result = await self.browser.begin_login(row.provider_id, row.profile_id, headless=headless)
        except BrowserUnavailable as exc:
            await self._record_failure(row.id, exc.message, code="login_begin_failed")
            raise AccountError(exc.message, code="login_begin_failed") from exc
        browser_status = str(result.get("login_state") or "")
        logged_in = bool(result.get("logged_in"))
        async with self.db.session_scope() as session:
            target = await session.get(AiAccountRow, account_id)
            assert target is not None
            if logged_in:
                target.status = STATUS_VERIFIED
                target.last_verified_at = utcnow()
                target.last_login_at = target.last_verified_at
                target.last_error = None
            else:
                target.status = STATUS_PENDING
            result = {**result, "account": _row_dict(target)}
        await self.audit.record(
            "account.login_begin",
            account_id=account_id,
            provider=row.provider_id,
            profile=row.profile_id,
            state=browser_status,
            headless=bool(result.get("headless")),
        )
        await self.events.publish(
            "account.login_opened",
            account_id=account_id,
            provider_id=row.provider_id,
            url=result.get("url"),
            important=True,
        )
        return result

    async def complete_login(self, account_id: str) -> dict[str, Any]:
        """Only marks the account usable when the browser runtime really sees a session."""
        row = await self.get_row(account_id)
        try:
            info = await self.browser.complete_login(row.provider_id, row.profile_id)
        except BrowserUnavailable as exc:
            await self._record_failure(row.id, exc.message, code="login_incomplete")
            raise AccountError(exc.message, code="login_incomplete") from exc
        return await self._absorb(row.id, info, mark_login=True)

    async def verify(self, account_id: str) -> dict[str, Any]:
        row = await self.get_row(account_id)
        try:
            info = await self.browser.check_account(row.provider_id, row.profile_id)
        except BrowserUnavailable as exc:
            await self._record_failure(row.id, exc.message, code="verify_failed")
            raise AccountError(exc.message, code="verify_failed") from exc
        return await self._absorb(row.id, info)

    async def _absorb(
        self, account_id: str, info: dict[str, Any], *, mark_login: bool = False
    ) -> dict[str, Any]:
        status = str(info.get("status") or STATUS_UNVERIFIED)
        now = utcnow()
        async with self.db.session_scope() as session:
            row = await session.get(AiAccountRow, account_id)
            if row is None:
                raise AccountError(f"No account '{account_id}'.", code="unknown_account")
            row.status = status
            row.last_verified_at = now
            if status == STATUS_VERIFIED:
                if mark_login or row.last_login_at is None:
                    row.last_login_at = now
                row.last_error = None
                row.last_error_at = None
            else:
                row.last_error = info.get("last_error") or f"Browser reported '{status}'."
                row.last_error_at = now
                if status == STATUS_RATE_LIMITED and row.active:
                    row.active = False
            stored = _row_dict(row)
        await self.audit.record(
            "account.verified",
            account_id=account_id,
            status=status,
            profile=stored["profile_id"],
        )
        await self.events.publish(
            "account.verified" if status == STATUS_VERIFIED else "account.unavailable",
            account_id=account_id,
            provider_id=stored["provider_id"],
            status=status,
            important=status != STATUS_VERIFIED,
        )
        return stored

    async def _record_failure(self, account_id: str, message: str, *, code: str) -> None:
        async with self.db.session_scope() as session:
            row = await session.get(AiAccountRow, account_id)
            if row is None:
                return
            row.last_error = message
            row.last_error_at = utcnow()
        await self.events.publish(
            "account.action_failed", account_id=account_id, code=code, error=message,
            important=True,
        )

    async def logout(self, account_id: str) -> dict[str, Any]:
        """The browser runtime exposes no sign-out call, so a human signs out in
        the visible window and only a real check afterwards may mark the account
        as signed out. Nothing here claims success it cannot see."""
        row = await self.get_row(account_id)
        was_signed_in = row.status == STATUS_VERIFIED
        try:
            opened = await self.browser.begin_login(row.provider_id, row.profile_id)
        except BrowserUnavailable as exc:
            raise AccountError(
                f"Cannot open the {row.provider_id} window to sign out: {exc.message}",
                code="logout_unreachable",
            ) from exc
        try:
            info = await self.browser.check_account(row.provider_id, row.profile_id)
        except BrowserUnavailable as exc:
            raise AccountError(exc.message, code="logout_unverified") from exc
        still_signed_in = str(info.get("status")) == STATUS_VERIFIED
        account = await self._absorb(account_id, info)
        if still_signed_in:
            return {
                "signed_out": False,
                "previously_signed_in": was_signed_in,
                "still_signed_in": True,
                "account": account,
                "window": {"url": opened.get("url"), "profile_id": row.profile_id},
                "detail": (
                    "The provider still reports this session as signed in. Sign out "
                    "inside the browser window that just opened, then press Logout again."
                ),
            }
        if not was_signed_in:
            return {
                "signed_out": False,
                "previously_signed_in": False,
                "still_signed_in": False,
                "account": account,
                "window": {"url": opened.get("url"), "profile_id": row.profile_id},
                "detail": (
                    "This account had no verified session to begin with, so there was "
                    "nothing to sign out of. The check above is the browser's real answer."
                ),
            }
        async with self.db.session_scope() as session:
            target = await session.get(AiAccountRow, account_id)
            if target is not None:
                target.active = False
        await self.audit.record(
            "account.logout", account_id=account_id, provider=row.provider_id,
            profile=row.profile_id,
        )
        await self.events.publish(
            "account.logout", account_id=account_id, provider_id=row.provider_id, important=True
        )
        return {
            "signed_out": True,
            "previously_signed_in": True,
            "still_signed_in": False,
            "account": account,
            "detail": "",
        }

    async def remove(self, account_id: str, *, confirm: bool, purge_profile: bool = True) -> dict:
        if not confirm:
            raise AccountError(
                "Removing an account is destructive: it deletes the browser profile and "
                "the signed-in session inside it. Send confirm=true to go ahead.",
                code="confirmation_required",
            )
        row = await self.get_row(account_id)
        profile_deleted = False
        profile_error: str | None = None
        if purge_profile:
            try:
                await self.browser.close_profile(row.profile_id)
            except BrowserUnavailable:
                pass  # a stopped profile is already fine to delete
            try:
                await self.browser.delete_profile(row.profile_id)
                profile_deleted = True
            except BrowserUnavailable as exc:
                profile_error = exc.message
        if profile_error:
            # The profile still holds a live session, so forgetting the row here
            # would strand it: keep the account and tell the operator why.
            raise AccountError(
                f"The account row was kept because the browser refused to delete "
                f"profile {row.profile_id}: {profile_error}",
                code="profile_delete_failed",
            )
        async with self.db.session_scope() as session:
            target = await session.get(AiAccountRow, account_id)
            if target is None:
                raise AccountError(f"No account '{account_id}'.", code="unknown_account")
            await session.delete(target)
            promoted = False
            if row.active:
                remaining = (
                    (
                        await session.execute(
                            select(AiAccountRow).where(
                                AiAccountRow.provider_id == row.provider_id,
                                AiAccountRow.status == STATUS_VERIFIED,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if remaining is not None:
                    remaining.active = True
                    promoted = True
        await self.audit.record(
            "account.removed",
            account_id=account_id,
            provider=row.provider_id,
            profile=row.profile_id,
            profile_deleted=profile_deleted,
        )
        await self.events.publish(
            "account.removed",
            account_id=account_id,
            provider_id=row.provider_id,
            profile_id=row.profile_id,
            important=True,
        )
        return {
            "removed": account_id,
            "profile_deleted": profile_deleted,
            "provider_id": row.provider_id,
            "active_account_promoted": promoted,
        }

    # ------------------------------------------------------------------ summaries
    async def provider_summary(self) -> dict[str, Any]:
        catalog = await self.provider_catalog()
        accounts = await self.list()
        profile_states: dict[str, str] = {}
        if catalog["connected"]:
            try:
                for profile in await self.browser.list_profiles():
                    profile_states[str(profile.get("id"))] = str(profile.get("active") or "")
            except BrowserUnavailable as exc:
                catalog = {**catalog, "connected": False, "error": exc.message}
        by_provider: dict[str, list[dict[str, Any]]] = {}
        for account in accounts:
            by_provider.setdefault(account["provider_id"], []).append(account)
        cards = []
        for provider in catalog["providers"]:
            owned = by_provider.get(provider["id"], [])
            active = next((item for item in owned if item["active"]), None)
            cards.append(
                {
                    **provider,
                    "accounts": owned,
                    "configured": len(owned),
                    "authenticated": sum(1 for item in owned if item["status"] == STATUS_VERIFIED),
                    "needs_attention": sum(
                        1 for item in owned if item["status"] not in (STATUS_VERIFIED,)
                    ),
                    "active_account": active,
                    "last_verified_at": max(
                        (
                            item["last_verified_at"]
                            for item in owned
                            if item["last_verified_at"]
                        ),
                        key=lambda value: datetime.fromisoformat(value),
                        default=None,
                    ),
                    "profiles_present": all(
                        item["profile_id"] in profile_states for item in owned
                    )
                    if profile_states and owned
                    else None,
                }
            )
        cards.sort(key=lambda item: (item["id"] not in PREFERRED_PROVIDERS, item["id"]))
        return {
            "connected": catalog["connected"],
            "error": catalog["error"],
            "providers": cards,
            "totals": {
                "accounts": len(accounts),
                "authenticated": sum(1 for item in accounts if item["status"] == STATUS_VERIFIED),
                "providers_with_accounts": len(by_provider),
            },
        }

    async def usage(self, account_id: str) -> dict[str, Any]:
        """Where this account is actually referenced, before anyone deletes it."""
        async with self.db.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(ParticipantRow, MeetingRow)
                        .join(MeetingRow, MeetingRow.id == ParticipantRow.meeting_id)
                        .where(ParticipantRow.account_id == account_id)
                        .order_by(MeetingRow.created_at.desc())
                        .limit(20)
                    )
                )
                .all()
            )
        return {
            "account_id": account_id,
            "meetings": [
                {
                    "meeting_id": participant.meeting_id,
                    "status": meeting.status,
                    "agenda": meeting.agenda[:160],
                    "created_at": iso(meeting.created_at),
                    "profile_id": participant.profile_id,
                }
                for participant, meeting in rows
            ],
        }

    async def status_counts(self) -> dict[str, int]:
        accounts = await self.list()
        counts: dict[str, int] = {}
        for account in accounts:
            counts[account["status"]] = counts.get(account["status"], 0) + 1
        counts["accounts"] = len(accounts)
        return counts


def profile_id_is_valid(candidate: str) -> bool:
    return bool(_VALID_PROFILE_ID.match(candidate))
