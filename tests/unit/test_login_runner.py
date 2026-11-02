"""Identity prefill runner: one failed cell must not poison the batch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgemesh_browser.accounts.login_runner import IdentityLoginRunner
from forgemesh_browser.accounts.models import IdentityInfo
from forgemesh_browser.accounts.session_health import STATUS_ERROR
from forgemesh_browser.core.exceptions import BrowserLimitError
from forgemesh_browser.settings import Settings


class _Providers:
    def name(self, provider_id: str) -> str:
        return provider_id.title()

    def get(self, provider_id: str):  # pragma: no cover - _login_one is patched
        raise AssertionError(f"unexpected provider lookup: {provider_id}")

    def list(self):
        return [SimpleNamespace(id="gemini", enabled=True), SimpleNamespace(id="claude", enabled=True)]


class _Browser:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def stop(self, profile_id: str) -> bool:
        self.stopped.append(profile_id)
        return True


class _Accounts:
    def __init__(self) -> None:
        self.providers = _Providers()
        self.browser = _Browser()


class _Identities:
    def __init__(self, identity: IdentityInfo) -> None:
        self._identity = identity

    async def get(self, email: str) -> IdentityInfo:
        return self._identity

    async def list(self) -> list[IdentityInfo]:
        return [self._identity]


def _runner(settings: Settings, identity: IdentityInfo) -> tuple[IdentityLoginRunner, _Accounts]:
    settings.identity_login_delay_seconds = 0
    accounts = _Accounts()
    runner = IdentityLoginRunner(
        settings,
        _Identities(identity),  # type: ignore[arg-type]
        accounts,  # type: ignore[arg-type]
        SimpleNamespace(publish=lambda *a, **k: None),  # type: ignore[arg-type]
        SimpleNamespace(record=lambda *a, **k: None),  # type: ignore[arg-type]
    )
    return runner, accounts


@pytest.mark.asyncio
async def test_one_cell_error_is_reported_without_aborting_the_batch(
    settings: Settings,
) -> None:
    identity = IdentityInfo(email="person@example.com", profile_id="prof_person")
    runner, accounts = _runner(settings, identity)
    calls: list[str] = []

    async def cell(_identity, provider_id, **kwargs):
        calls.append(provider_id)
        if provider_id == "gemini":
            raise BrowserLimitError(
                "Maximum concurrent profiles reached (6). Stop a profile first."
            )
        from forgemesh_browser.accounts.models import IdentityLoginResult

        return IdentityLoginResult(
            email=_identity.email,
            profile_id=_identity.profile_id,
            provider_id=provider_id,
            status="logged_in",
            headless=kwargs.get("headless", False),
        )

    runner._login_one = cell  # type: ignore[method-assign]
    results = await runner.login(
        "person@example.com", provider_ids=["gemini", "claude"], headless=True
    )

    assert calls == ["gemini", "claude"]
    assert results[0].status == STATUS_ERROR
    assert "Maximum concurrent profiles" in results[0].detail
    assert results[1].status == "logged_in"
    # Headless profiles are released after every cell so a batch stays under
    # the browser-context cap.
    assert accounts.browser.stopped == ["prof_person", "prof_person"]


@pytest.mark.asyncio
async def test_headed_login_leaves_the_window_open_for_verification(
    settings: Settings,
) -> None:
    identity = IdentityInfo(email="person@example.com", profile_id="prof_person")
    runner, accounts = _runner(settings, identity)

    async def cell(_identity, provider_id, **kwargs):
        raise BrowserLimitError("still limited")

    runner._login_one = cell  # type: ignore[method-assign]
    summary = await runner.login_all(
        emails=["person@example.com"], provider_ids=["gemini"], headless=False
    )

    assert summary.total == 1
    assert summary.errors == 1
    assert accounts.browser.stopped == []
