"""Account registry: manual login, persistent sessions, availability and asks.

Policy: profiles exist for legitimate separation of accounts. This module has
no account rotation, no quota evasion, and never switches to a paid API on its
own. A rate limit stops the workflow and records the provider's own message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError

from forgemesh_browser.accounts import session_health
from forgemesh_browser.accounts.models import AccountInfo, AccountStatusSummary
from forgemesh_browser.accounts.session_health import (
    STATUS_AVAILABLE,
    STATUS_ERROR,
    STATUS_LOGIN_PENDING,
    STATUS_NEEDS_REAUTH,
    STATUS_RATE_LIMITED,
)
from forgemesh_browser.core.browser_manager import BrowserManager
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import (
    AdapterError,
    NotAuthenticated,
    RateLimited,
)
from forgemesh_browser.media.asset_storage import AssetStore
from forgemesh_browser.media.uploads import UploadManager
from forgemesh_browser.profiles.manager import ProfileManager
from forgemesh_browser.providers.base import GenericChatProvider, same_site
from forgemesh_browser.providers.registry import ProviderRegistry
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database
from forgemesh_browser.storage.models import AccountRow
from forgemesh_browser.storage.repositories import accounts as account_repo

logger = logging.getLogger("forgemesh.accounts")


class AccountManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        profiles: ProfileManager,
        browser: BrowserManager,
        providers: ProviderRegistry,
        uploads: UploadManager,
        assets: AssetStore,
        events: EventBus,
        audit: AuditLogger,
    ) -> None:
        self.settings = settings
        self.db = db
        self.profiles = profiles
        self.browser = browser
        self.providers = providers
        self.uploads = uploads
        self.assets = assets
        self.events = events
        self.audit = audit
        # One browser profile must run one request at a time. The control plane
        # cancels its HTTP call on timeout but the server-side ask can still be
        # typing/collecting; a second ask would corrupt that baseline.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, profile_id: str) -> asyncio.Lock:
        lock = self._locks.get(profile_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[profile_id] = lock
        return lock

    # ------------------------------------------------------------------ queries
    def _provider_name(self, provider_id: str) -> str:
        for info in self.providers.list():
            if info.id == provider_id:
                return info.name
        return provider_id

    def _info(self, row: AccountRow) -> AccountInfo:
        return session_health.account_info(row, self._provider_name(row.provider_id))

    async def list_accounts(
        self, *, profile_id: str | None = None, provider_id: str | None = None
    ) -> list[AccountInfo]:
        async with self.db.session_scope() as session:
            rows = await account_repo.list_all(
                session, profile_id=profile_id, provider_id=provider_id
            )
            return [self._info(row) for row in rows]

    async def get(self, profile_id: str, provider_id: str) -> AccountInfo:
        async with self.db.session_scope() as session:
            row = await account_repo.get(session, profile_id, provider_id)
            if row is None:
                raise NotAuthenticated(
                    f"No account registered for {provider_id} in profile '{profile_id}'. "
                    f"Run: fmb account login {provider_id} --profile {profile_id}"
                )
            return self._info(row)

    async def summary(
        self, *, profile_id: str | None = None
    ) -> AccountStatusSummary:
        return session_health.summarize(
            await self.list_accounts(profile_id=profile_id)
        )

    async def _set_status(
        self,
        profile_id: str,
        provider_id: str,
        status: str,
        *,
        error: str | None = None,
        retry_after: datetime | None = None,
        ok: bool = False,
    ) -> AccountInfo:
        async with self.db.session_scope() as session:
            row = await account_repo.upsert(session, profile_id, provider_id)
            updated = await account_repo.update_status(
                session,
                row.id,
                status,
                error=error,
                retry_after=retry_after,
                ok=ok,
            )
            # Do not call session.refresh() here: it reloads the row from the
            # database before the pending UPDATE is committed and silently
            # discards the new status.
            if updated is not None:
                row = updated
            return self._info(row)

    async def record_status(
        self,
        profile_id: str,
        provider_id: str,
        status: str,
        *,
        error: str | None = None,
        ok: bool = False,
    ) -> AccountInfo:
        """Public status hook for the identity prefill runner."""

        return await self._set_status(
            profile_id, provider_id, status, error=error, ok=ok
        )

    # ------------------------------------------------------------------- login
    async def begin_login(
        self, profile_id: str, provider_id: str, *, headless: bool = False
    ) -> dict[str, Any]:
        await self.profiles.require(profile_id)
        adapter = self.providers.get(provider_id)

        if self.browser.headless_mode(profile_id) is True and not headless:
            await self.browser.stop(profile_id)
        if not self.browser.is_running(profile_id):
            pb = await self.browser.start(profile_id, headless=headless)
            headless = pb.headless
        else:
            headless = bool(self.browser.headless_mode(profile_id))
        page = await self.browser.page(profile_id)
        await self.browser.goto(profile_id, adapter.entry_url)
        page = await self.browser.newest_page(profile_id)
        state = await adapter.detect_login_state(page)
        # A login window is now open, so a stale error/needs_reauth row must be
        # moved to login_pending; only an already valid session becomes
        # available.
        if state.logged_in:
            info = await self._set_status(
                profile_id, provider_id, STATUS_AVAILABLE, ok=True
            )
        else:
            info = await self._set_status(
                profile_id, provider_id, STATUS_LOGIN_PENDING
            )
        await self.audit.record(
            "account.login_begin",
            details={
                "profile": profile_id,
                "provider": provider_id,
                "headless": headless,
                "state": state.state,
            },
        )
        self.events.publish(
            "account.login_begin",
            profile=profile_id,
            provider=provider_id,
            state=state.state,
        )
        return {
            "profile_id": profile_id,
            "provider_id": provider_id,
            "provider_name": adapter.display_name,
            "url": page.url,
            "tab": self.browser.tab_id_for(page, profile_id),
            "headless": headless,
            "login_state": state.state,
            "logged_in": state.logged_in,
            "account_id": info.id,
            "instructions": (
                "Log in manually in the browser window. Passwords are never sent "
                "through the terminal. Complete CAPTCHA/MFA yourself."
            ),
        }

    async def complete_login(self, profile_id: str, provider_id: str) -> AccountInfo:
        await self.profiles.require(profile_id)
        adapter = self.providers.get(provider_id)
        if not self.browser.is_running(profile_id):
            await self.browser.start(profile_id, headless=self.settings.headless)
        # Manual sign-in may have happened in a popup or a different tab, so
        # always validate the most recent tab after returning to the app root.
        await self.browser.goto(profile_id, adapter.home_url)
        page = await self.browser.newest_page(profile_id)
        state = await adapter.detect_login_state(page)
        if not state.logged_in:
            info = await self._set_status(
                profile_id,
                provider_id,
                STATUS_NEEDS_REAUTH,
                error=f"Login not detected ({state.state}: {state.detail})",
            )
            await self.audit.record(
                "account.login_failed",
                details={"profile": profile_id, "provider": provider_id, "state": state.state},
            )
            if state.state == "blocked":
                raise NotAuthenticated(
                    f"{adapter.display_name}'s identity provider refused this automated "
                    f"browser at {state.url}. ForgeMesh does not bypass security checks. "
                    "Sign in with the site's own email/phone form, or use the provider's "
                    "official API with an API key."
                )
            raise NotAuthenticated(
                f"Login for {adapter.display_name} was not detected. Finish signing in "
                f"in the browser window and run this command again."
            )
        info = await self._set_status(profile_id, provider_id, STATUS_AVAILABLE, ok=True)
        await self.audit.record(
            "account.login_complete",
            details={"profile": profile_id, "provider": provider_id},
        )
        self.events.publish(
            "account.available", profile=profile_id, provider=provider_id
        )
        return info

    # ------------------------------------------------------------------- health
    async def check(self, profile_id: str, provider_id: str) -> AccountInfo:
        await self.profiles.require(profile_id)
        async with self._lock_for(profile_id):
            return await self._check_locked(profile_id, provider_id)

    async def _check_locked(self, profile_id: str, provider_id: str) -> AccountInfo:
        adapter = self.providers.get(provider_id)
        if not self.browser.is_running(profile_id):
            await self.browser.start(profile_id, headless=self.settings.headless)
        page = await self.browser.page(profile_id)
        try:
            if not same_site(page.url, adapter.home_url):
                await self.browser.goto(profile_id, adapter.home_url)
            state = await adapter.detect_login_state(page)
        except PlaywrightError as exc:
            info = await self._set_status(
                profile_id, provider_id, STATUS_ERROR, error=f"Browser error: {exc}"
            )
            return info
        if state.logged_in:
            return await self._set_status(
                profile_id, provider_id, STATUS_AVAILABLE, ok=True
            )
        if state.state == "logged_out":
            return await self._set_status(
                profile_id,
                provider_id,
                STATUS_NEEDS_REAUTH,
                error=f"Session expired ({state.detail})",
            )
        return await self._set_status(
            profile_id,
            provider_id,
            STATUS_ERROR,
            error=f"Could not determine login state ({state.detail})",
        )

    async def check_all(self, *, profile_id: str | None = None) -> list[AccountInfo]:
        accounts = await self.list_accounts(profile_id=profile_id)
        results = []
        for account in accounts:
            try:
                results.append(await self.check(account.profile_id, account.provider_id))
            except Exception as exc:
                logger.warning(
                    "Health check failed for %s/%s: %s",
                    account.profile_id,
                    account.provider_id,
                    exc,
                )
                results.append(account)
        return results

    # --------------------------------------------------------------- automation
    async def _page_for(
        self, adapter: GenericChatProvider, profile_id: str
    ):
        if not self.browser.is_running(profile_id):
            await self.browser.start(profile_id, headless=self.settings.headless)
        page = await self.browser.page(profile_id)
        if not same_site(page.url, adapter.home_url):
            await self.browser.goto(profile_id, adapter.home_url)
        return page

    async def ask(
        self,
        profile_id: str,
        provider_id: str,
        prompt: str,
        *,
        files: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        await self.profiles.require(profile_id)
        async with self._lock_for(profile_id):
            return await self._ask_locked(
                profile_id,
                provider_id,
                prompt,
                files=files,
                timeout_ms=timeout_ms,
            )

    async def _ask_locked(
        self,
        profile_id: str,
        provider_id: str,
        prompt: str,
        *,
        files: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        adapter = self.providers.get(provider_id)
        if not prompt.strip() and not files:
            raise AdapterError("Provide a prompt or at least one file")
        prepared: list[Path] = []
        for item in files or []:
            path, _meta = self.uploads.prepare(item)
            prepared.append(path)

        try:
            page = await self._page_for(adapter, profile_id)
        except PlaywrightError as exc:
            # Opening the tab happens before the automation block, so a dead
            # browser used to escape as an unhandled 500 with no explanation.
            await self._set_status(
                profile_id, provider_id, STATUS_ERROR, error=f"Browser error: {exc}"
            )
            raise AdapterError(
                f"{adapter.display_name}: could not open a browser tab: {exc}"
            ) from exc
        try:
            await adapter.require_login(page)
            submission = await adapter.submit_prompt(page, prompt, files=prepared)
            await asyncio.sleep(0.4)
            restriction = await adapter.detect_restriction(page)
            if restriction is not None:
                await self._record_limited(profile_id, provider_id, restriction)
                raise restriction
            response = await adapter.collect_response(
                page,
                baseline=submission["response_baseline"],
                timeout_ms=timeout_ms,
                idle_expected=bool(submission.get("send_control")),
            )
            returned = str(response.get("text") or "").strip()
            if returned and prompt.strip() and returned == prompt.strip():
                # Some role-agnostic selectors can match the user's own bubble.
                # Returning it would be a fabricated answer.
                raise AdapterError(
                    f"{adapter.display_name}: the page returned the prompt, not an answer."
                )
        except RateLimited as exc:
            await self._record_limited(profile_id, provider_id, exc)
            raise
        except NotAuthenticated:
            await self._set_status(
                profile_id, provider_id, STATUS_NEEDS_REAUTH, error="Session expired"
            )
            raise
        except AdapterError as exc:
            await self._set_status(profile_id, provider_id, STATUS_ERROR, error=str(exc))
            raise
        except PlaywrightError as exc:
            await self._set_status(
                profile_id, provider_id, STATUS_ERROR, error=f"Browser error: {exc}"
            )
            raise AdapterError(
                f"{adapter.display_name}: browser error while asking: {exc}"
            ) from exc

        await self._set_status(profile_id, provider_id, STATUS_AVAILABLE, ok=True)
        await self.audit.record(
            "account.ask",
            details={
                "profile": profile_id,
                "provider": provider_id,
                "prompt_chars": len(prompt),
                "files": [path.name for path in prepared],
            },
        )
        self.events.publish(
            "account.response",
            profile=profile_id,
            provider=provider_id,
            chars=response["chars"],
        )
        return {
            "profile_id": profile_id,
            "provider_id": provider_id,
            "provider_name": adapter.display_name,
            "submitted_via": submission["sent_via"],
            "attached_files": [str(path) for path in prepared],
            **response,
        }

    async def _record_limited(
        self, profile_id: str, provider_id: str, exc: RateLimited
    ) -> None:
        await self._set_status(
            profile_id,
            provider_id,
            STATUS_RATE_LIMITED,
            error=exc.message,
            retry_after=exc.retry_after,
        )
        await self.audit.record(
            "account.rate_limited",
            details={
                "profile": profile_id,
                "provider": provider_id,
                "message": exc.message,
                "retry_after": exc.retry_after.isoformat() if exc.retry_after else None,
            },
        )
        self.events.publish(
            "account.rate_limited",
            profile=profile_id,
            provider=provider_id,
            retry_after=exc.retry_after.isoformat() if exc.retry_after else None,
        )

    async def capture_response(
        self, profile_id: str, provider_id: str, output: str | Path | None = None
    ) -> dict[str, Any]:
        await self.profiles.require(profile_id)
        adapter = self.providers.get(provider_id)
        page = await self._page_for(adapter, profile_id)
        await adapter.require_login(page)
        target = self.settings.results_dir / (
            f"{provider_id}-{profile_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        )
        if output:
            target = Path(output).expanduser()
            if not target.is_absolute():
                target = self.settings.results_dir / target
        info = await adapter.capture_response(page, target)
        asset = await self.assets.record_reference(
            Path(info["path"]),
            kind="result",
            source_url=page.url,
            provider_id=provider_id,
            profile_id=profile_id,
        )
        if asset:
            info["asset_id"] = asset["id"]
        return info
