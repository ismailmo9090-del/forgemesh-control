"""Identity × provider prefill login matrix.

The runner fills the site's own email/password form from the encrypted vault,
then waits for the session to appear. It never solves a CAPTCHA, never submits
an OTP, and never retries a rejected password: those cells become
``needs_human`` or ``needs_credential`` and the batch moves on.

Results never contain the password. What is persisted is the ordinary account
status (``available`` / ``needs_reauth``) plus an audit entry.
"""

from __future__ import annotations

import asyncio
import logging

from forgemesh_browser.accounts.identities import IdentityManager
from forgemesh_browser.accounts.manager import AccountManager
from forgemesh_browser.accounts.models import (
    IdentityInfo,
    IdentityLoginResult,
    IdentityLoginSummary,
)
from forgemesh_browser.accounts.session_health import (
    STATUS_AVAILABLE,
    STATUS_ERROR,
    STATUS_NEEDS_REAUTH,
)
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import (
    AccountError,
    AdapterError,
    CredentialMissing,
    NeedsHuman,
    NotAuthenticated,
)
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.settings import Settings

logger = logging.getLogger("forgemesh.identity_login")

STATUS_ALREADY_LOGGED_IN = "already_logged_in"
STATUS_LOGGED_IN = "logged_in"
STATUS_NEEDS_HUMAN = "needs_human"
STATUS_NEEDS_CREDENTIAL = "needs_credential"
STATUS_BLOCKED = "blocked"

_POLL_INTERVAL_S = 2.0


class IdentityLoginRunner:
    def __init__(
        self,
        settings: Settings,
        identities: IdentityManager,
        accounts: AccountManager,
        events: EventBus,
        audit: AuditLogger,
    ) -> None:
        self.settings = settings
        self.identities = identities
        self.accounts = accounts
        self.events = events
        self.audit = audit

    # ------------------------------------------------------------------ public
    async def login(
        self,
        email: str,
        *,
        provider_ids: list[str] | None = None,
        headless: bool = False,
        timeout_s: int | None = None,
    ) -> list[IdentityLoginResult]:
        identity = await self.identities.get(email)
        providers = provider_ids or self.enabled_provider_ids()
        results: list[IdentityLoginResult] = []
        for provider_id in providers:
            results.append(
                await self._login_cell(
                    identity,
                    provider_id,
                    headless=headless,
                    timeout_s=timeout_s,
                )
            )
            if headless:
                await self._stop_quietly(identity.profile_id)
            await self._pause()
        return results

    async def login_all(
        self,
        *,
        emails: list[str] | None = None,
        provider_ids: list[str] | None = None,
        headless: bool = False,
        timeout_s: int | None = None,
    ) -> IdentityLoginSummary:
        identities = await self.identities.list()
        if emails:
            wanted = {email.strip().lower() for email in emails}
            identities = [item for item in identities if item.email in wanted]
            missing = wanted - {item.email for item in identities}
            if missing:
                raise NotAuthenticated(
                    "Unknown identities: " + ", ".join(sorted(missing))
                )
        providers = provider_ids or self.enabled_provider_ids()
        summary = IdentityLoginSummary()
        for identity in identities:
            for provider_id in providers:
                result = await self._login_cell(
                    identity,
                    provider_id,
                    headless=headless,
                    timeout_s=timeout_s,
                )
                summary.results.append(result)
                self._count(summary, result)
                if headless:
                    await self._stop_quietly(identity.profile_id)
                await self._pause()
        return summary

    def enabled_provider_ids(self) -> list[str]:
        return [
            info.id for info in self.accounts.providers.list() if info.enabled
        ]

    # ----------------------------------------------------------------- internal
    async def _login_cell(
        self,
        identity: IdentityInfo,
        provider_id: str,
        *,
        headless: bool,
        timeout_s: int | None,
    ) -> IdentityLoginResult:
        """Run one cell; any failure is reported without aborting the batch."""

        try:
            return await self._login_one(
                identity,
                provider_id,
                headless=headless,
                timeout_s=timeout_s,
            )
        except AccountError as exc:
            detail = exc.message
        except Exception as exc:  # noqa: BLE001 - one cell must not abort the batch
            logger.warning(
                "Login cell failed for %s/%s: %s",
                identity.email,
                provider_id,
                exc,
            )
            detail = f"{type(exc).__name__}: {exc}"
        return IdentityLoginResult(
            email=identity.email,
            profile_id=identity.profile_id,
            provider_id=provider_id,
            provider_name=self.accounts.providers.name(provider_id),
            status=STATUS_ERROR,
            detail=detail,
            headless=headless,
        )

    async def _stop_quietly(self, profile_id: str) -> None:
        """Release a headless profile after a cell so batches stay under the cap."""

        try:
            await self.accounts.browser.stop(profile_id)
        except Exception as exc:  # noqa: BLE001 - cleanup must never fail the batch
            logger.debug("Could not stop profile %s after login cell: %s", profile_id, exc)

    def _count(self, summary: IdentityLoginSummary, result: IdentityLoginResult) -> None:
        summary.total += 1
        status = result.status
        if status == STATUS_ALREADY_LOGGED_IN:
            summary.already_logged_in += 1
        elif status == STATUS_LOGGED_IN:
            summary.logged_in += 1
        elif status == STATUS_NEEDS_HUMAN:
            summary.needs_human += 1
        elif status == STATUS_NEEDS_CREDENTIAL:
            summary.needs_credential += 1
        elif status == STATUS_BLOCKED:
            summary.blocked += 1
        else:
            summary.errors += 1

    async def _pause(self) -> None:
        if self.settings.identity_login_delay_seconds > 0:
            await asyncio.sleep(self.settings.identity_login_delay_seconds)

    def _result(
        self,
        identity: IdentityInfo,
        provider_id: str,
        *,
        status: str,
        detail: str = "",
        url: str = "",
        filled: list[str] | None = None,
        headless: bool = False,
    ) -> IdentityLoginResult:
        return IdentityLoginResult(
            email=identity.email,
            profile_id=identity.profile_id,
            provider_id=provider_id,
            provider_name=self.accounts.providers.name(provider_id),
            status=status,
            detail=detail,
            url=url,
            filled=filled or [],
            headless=headless,
        )

    async def _record(
        self,
        identity: IdentityInfo,
        provider_id: str,
        result: IdentityLoginResult,
    ) -> None:
        if result.status in {STATUS_LOGGED_IN, STATUS_ALREADY_LOGGED_IN}:
            await self.accounts.record_status(
                identity.profile_id, provider_id, STATUS_AVAILABLE, ok=True
            )
            await self.identities.touch_login(identity.email)
        elif result.status == STATUS_ERROR:
            await self.accounts.record_status(
                identity.profile_id, provider_id, STATUS_ERROR, error=result.detail
            )
        else:
            await self.accounts.record_status(
                identity.profile_id,
                provider_id,
                STATUS_NEEDS_REAUTH,
                error=result.detail or result.status,
            )
        await self.audit.record(
            "identity.login",
            details={
                "email": identity.email,
                "profile": identity.profile_id,
                "provider": provider_id,
                "status": result.status,
                "filled": result.filled,
                "headless": result.headless,
            },
        )
        self.events.publish(
            "identity.login",
            email=identity.email,
            profile=identity.profile_id,
            provider=provider_id,
            status=result.status,
        )

    async def _login_one(
        self,
        identity: IdentityInfo,
        provider_id: str,
        *,
        headless: bool = False,
        timeout_s: int | None = None,
    ) -> IdentityLoginResult:
        adapter = self.accounts.providers.get(provider_id)
        profile_id = identity.profile_id
        await self.accounts.profiles.require(profile_id)
        timeout_s = timeout_s or self.settings.identity_login_timeout_seconds

        browser = self.accounts.browser
        if browser.headless_mode(profile_id) is True and not headless:
            await browser.stop(profile_id)
        if not browser.is_running(profile_id):
            started = await browser.start(profile_id, headless=headless)
            headless = started.headless
        else:
            headless = bool(browser.headless_mode(profile_id))

        # Check the normal app first. Going straight to /login would submit the
        # credentials again even when a valid session already exists, which is
        # exactly the kind of unnecessary auth attempt we want to avoid.
        await browser.goto(profile_id, adapter.home_url)
        page = await browser.newest_page(profile_id)
        state = await adapter.detect_login_state(page)
        if state.logged_in:
            result = self._result(
                identity,
                provider_id,
                status=STATUS_ALREADY_LOGGED_IN,
                detail="session already valid",
                url=state.url,
                headless=headless,
            )
            await self._record(identity, provider_id, result)
            return result

        if adapter.entry_url.rstrip("/") != adapter.home_url.rstrip("/"):
            await browser.goto(profile_id, adapter.entry_url)
            page = await browser.newest_page(profile_id)
            state = await adapter.detect_login_state(page)
            if state.logged_in:
                result = self._result(
                    identity,
                    provider_id,
                    status=STATUS_ALREADY_LOGGED_IN,
                    detail="session already valid",
                    url=state.url,
                    headless=headless,
                )
                await self._record(identity, provider_id, result)
                return result

        obstacle = await adapter.detect_login_obstacle(page)
        if obstacle is not None:
            result = self._result(
                identity,
                provider_id,
                status=STATUS_NEEDS_HUMAN
                if obstacle.needs_human
                else STATUS_NEEDS_CREDENTIAL,
                detail=obstacle.detail,
                url=obstacle.url,
                headless=headless,
            )
            await self._record(identity, provider_id, result)
            return result

        start_selector = await adapter.open_login_form(page)
        page = await browser.newest_page(profile_id)
        if start_selector:
            await asyncio.sleep(0.5)
            page = await browser.newest_page(profile_id)

        obstacle = await adapter.detect_login_obstacle(page)
        if obstacle is not None:
            result = self._result(
                identity,
                provider_id,
                status=STATUS_NEEDS_HUMAN
                if obstacle.needs_human
                else STATUS_NEEDS_CREDENTIAL,
                detail=obstacle.detail,
                url=obstacle.url,
                headless=headless,
            )
            await self._record(identity, provider_id, result)
            return result

        state = await adapter.detect_login_state(page)
        if state.state == "blocked":
            result = self._result(
                identity,
                provider_id,
                status=STATUS_BLOCKED,
                detail=(
                    "the identity provider refused this automated browser; "
                    "ForgeMesh does not bypass that check"
                ),
                url=state.url,
                headless=headless,
            )
            await self._record(identity, provider_id, result)
            return result
        if state.logged_in:
            result = self._result(
                identity,
                provider_id,
                status=STATUS_ALREADY_LOGGED_IN,
                detail="session already valid",
                url=state.url,
                headless=headless,
            )
            await self._record(identity, provider_id, result)
            return result

        try:
            password = self.identities.vault.require_password(
                identity.email, provider_id
            )
        except CredentialMissing as exc:
            result = self._result(
                identity,
                provider_id,
                status=STATUS_NEEDS_CREDENTIAL,
                detail=exc.message,
                url=page.url,
                headless=headless,
            )
            await self._record(identity, provider_id, result)
            return result

        try:
            prefill = await adapter.prefill_login(page, identity.email, password)
        except NeedsHuman as exc:
            result = self._result(
                identity,
                provider_id,
                status=STATUS_NEEDS_HUMAN,
                detail=exc.message,
                url=page.url,
                headless=headless,
            )
            await self._record(identity, provider_id, result)
            return result
        except AdapterError as exc:
            result = self._result(
                identity,
                provider_id,
                status=STATUS_ERROR,
                detail=exc.message,
                url=page.url,
                headless=headless,
            )
            await self._record(identity, provider_id, result)
            return result
        finally:
            # Drop the reference as soon as possible; it is never logged or
            # returned, but this avoids keeping it alive longer than needed.
            password = ""

        filled = list(prefill.get("filled", []))

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_S)
            page = await browser.newest_page(profile_id)
            obstacle = await adapter.detect_login_obstacle(page)
            if obstacle is not None:
                if obstacle.kind == "captcha":
                    status, detail = (
                        STATUS_NEEDS_HUMAN,
                        f"CAPTCHA after submit ({obstacle.detail}). "
                        "Complete it in the browser window.",
                    )
                elif obstacle.kind == "mfa":
                    status, detail = (
                        STATUS_NEEDS_HUMAN,
                        f"two-factor/verification step ({obstacle.detail}). "
                        "Finish it in the browser window.",
                    )
                else:
                    status, detail = (
                        STATUS_NEEDS_CREDENTIAL,
                        "the site rejected the stored email/password. Update it with "
                        "`fmb identity password <email> --provider "
                        f"{provider_id}`.",
                    )
                result = self._result(
                    identity,
                    provider_id,
                    status=status,
                    detail=detail,
                    url=page.url,
                    filled=filled,
                    headless=headless,
                )
                await self._record(identity, provider_id, result)
                return result
            state = await adapter.detect_login_state(page)
            if state.logged_in:
                result = self._result(
                    identity,
                    provider_id,
                    status=STATUS_LOGGED_IN,
                    detail=prefill.get("submit_via", ""),
                    url=page.url,
                    filled=filled,
                    headless=headless,
                )
                await self._record(identity, provider_id, result)
                return result
            if state.state == "blocked":
                result = self._result(
                    identity,
                    provider_id,
                    status=STATUS_BLOCKED,
                    detail=(
                        "the identity provider refused this automated browser; "
                        "ForgeMesh does not bypass that check"
                    ),
                    url=state.url,
                    filled=filled,
                    headless=headless,
                )
                await self._record(identity, provider_id, result)
                return result

        detail = (
            f"sign-in was not confirmed within {timeout_s}s. The browser "
            f"stays open in profile '{profile_id}'"
        )
        if headless:
            detail += "; re-run with --headed to finish verification"
        else:
            detail += "; finish any verification there, then run fmb identity status"
        result = self._result(
            identity,
            provider_id,
            status=STATUS_NEEDS_HUMAN,
            detail=detail,
            url=page.url,
            filled=filled,
            headless=headless,
        )
        await self._record(identity, provider_id, result)
        return result
