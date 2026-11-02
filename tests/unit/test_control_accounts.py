"""AI provider account registry (``forgemesh_control.accounts``).

Mocked in one place only: ``FakeBrowser`` stands in for the ForgeMesh Browser
runtime's HTTP API, so no network, no real provider account and no real
credential is involved. Everything else is the real service - real SQLite
rows, real profile ids, real status transitions and the real audit/event
trail. The point of these tests is the honesty contract: the service may only
report a session as usable when the browser runtime says so, and it may never
carry a cookie, token or password.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from forgemesh_control.accounts import (
    STATUS_PENDING,
    STATUS_RATE_LIMITED,
    STATUS_VERIFIED,
    AccountError,
    AiAccountService,
)
from forgemesh_control.audit import AuditService
from forgemesh_control.browser import BrowserUnavailable
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.storage import ControlDatabase

PROVIDERS = [
    {"id": "chatgpt", "name": "ChatGPT", "url": "https://chatgpt.com", "enabled": True,
     "login_supported": True},
    {"id": "deepseek", "name": "DeepSeek", "url": "https://chat.deepseek.com", "enabled": True,
     "login_supported": True},
]


class FakeBrowser:
    """The subset of the browser runtime API the account service calls."""

    def __init__(self) -> None:
        self.offline_for: set[str] = set()
        self.profiles: dict[str, dict] = {}
        self.calls: list[str] = []
        #: What the runtime reports when a login window opens.
        self.session_visible = False
        #: What the runtime's own session check reports.
        self.reported_status = "unverified"
        self.reported_error: str | None = None

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if name in self.offline_for:
            raise BrowserUnavailable("ForgeMesh Browser runtime is not reachable")

    async def providers(self) -> list[dict]:
        self._guard("providers")
        return [dict(item) for item in PROVIDERS]

    async def list_profiles(self) -> list[dict]:
        self._guard("list_profiles")
        return [dict(item) for item in self.profiles.values()]

    async def create_profile(self, profile_id: str, *, name: str = "", description: str = "") -> dict:
        self._guard("create_profile")
        record = {"id": profile_id, "name": name, "description": description}
        self.profiles[profile_id] = record
        return dict(record)

    async def rename_profile(self, profile_id: str, name: str) -> dict:
        self._guard("rename_profile")
        self.profiles[profile_id]["name"] = name
        return dict(self.profiles[profile_id])

    async def close_profile(self, profile_id: str) -> dict:
        self._guard("close_profile")
        return {"closed": profile_id}

    async def delete_profile(self, profile_id: str) -> dict:
        self._guard("delete_profile")
        self.profiles.pop(profile_id, None)
        return {"deleted": profile_id}

    async def begin_login(self, provider_id: str, profile_id: str, *, headless: bool = False) -> dict:
        self._guard("begin_login")
        return {
            "logged_in": self.session_visible,
            "login_state": "signed_in" if self.session_visible else "login_page",
            "url": f"https://example.invalid/login?profile={profile_id}",
            "headless": headless,
        }

    async def complete_login(self, provider_id: str, profile_id: str) -> dict:
        self._guard("complete_login")
        return self._check_payload()

    async def check_account(self, provider_id: str, profile_id: str) -> dict:
        self._guard("check_account")
        return self._check_payload()

    def _check_payload(self) -> dict:
        payload: dict = {"status": self.reported_status}
        if self.reported_error:
            payload["last_error"] = self.reported_error
        return payload


@pytest.fixture
def settings(tmp_path: Path) -> ControlSettings:
    resolved = ControlSettings(data_dir=tmp_path / "data", config_dir=tmp_path / "config")
    resolved.ensure_dirs()
    return resolved


@pytest.fixture
async def env(settings: ControlSettings):
    database = ControlDatabase(settings.db_path)
    await database.init()
    events = EventStore(database)
    audit = AuditService(database, settings.logs_dir)
    browser = FakeBrowser()
    service = AiAccountService(settings, database, events, audit, browser)
    try:
        yield SimpleNamespace(settings=settings, db=database, events=events, browser=browser,
                              service=service)
    finally:
        await database.close()


async def test_add_registers_an_unverified_account_with_its_own_profile(env) -> None:
    account = await env.service.add("chatgpt", label="personal")

    assert account["status"] == "unverified"
    assert account["verified"] is False
    assert account["active"] is True
    assert account["profile_id"].startswith("chatgpt-")
    assert "create_profile" in env.browser.calls
    assert account["profile_id"] in env.browser.profiles


async def test_first_account_only_is_active_and_activation_moves_the_flag(env) -> None:
    await env.service.add("chatgpt", label="one")
    second = await env.service.add("chatgpt", label="two")
    assert second["active"] is False

    activated = await env.service.set_active(second["id"])
    assert activated["active"] is True
    listed = {item["label"]: item for item in await env.service.list(provider_id="chatgpt")}
    assert listed["two"]["active"] is True
    assert listed["one"]["active"] is False


async def test_duplicate_label_in_the_same_provider_is_refused(env) -> None:
    await env.service.add("chatgpt", label="same")
    with pytest.raises(AccountError) as caught:
        await env.service.add("chatgpt", label="same")
    assert caught.value.code == "duplicate_label"


async def test_the_email_field_is_never_a_place_for_a_password(env) -> None:
    with pytest.raises(AccountError) as caught:
        await env.service.add("chatgpt", label="x", email="hunter2 my password")
    assert caught.value.code == "invalid_email"

    stored = await env.service.add("chatgpt", label="ok", email="me@example.com")
    assert stored["email"] == "me@example.com"


async def test_serialised_account_carries_no_credential_field(env) -> None:
    account = await env.service.add("chatgpt", label="personal")

    forbidden = ("cookie", "token", "password", "secret", "csrf")
    assert not [key for key in account if any(part in key for part in forbidden)]
    assert not [part for part in forbidden if part in str(account).lower()]


async def test_begin_login_only_trusts_what_the_runtime_sees(env) -> None:
    account = await env.service.add("chatgpt", label="personal")

    env.browser.session_visible = False
    opened = await env.service.begin_login(account["id"])
    assert opened["account"]["status"] == STATUS_PENDING
    assert opened["account"]["verified"] is False

    env.browser.session_visible = True
    opened = await env.service.begin_login(account["id"])
    assert opened["account"]["status"] == STATUS_VERIFIED
    assert opened["account"]["verified"] is True


async def test_completion_without_a_real_session_is_not_marked_signed_in(env) -> None:
    account = await env.service.add("chatgpt", label="personal")
    await env.service.begin_login(account["id"])
    env.browser.reported_status = "needs_reauth"
    env.browser.reported_error = "No session found on the provider page."

    result = await env.service.complete_login(account["id"])

    assert result["status"] != STATUS_VERIFIED
    assert result["verified"] is False
    assert result["last_error"] == "No session found on the provider page."


async def test_verification_absorbs_the_runtime_status_and_deactivates_limited_accounts(env) -> None:
    account = await env.service.add("deepseek", label="work")
    assert (await env.service.get(account["id"]))["active"] is True

    env.browser.reported_status = STATUS_RATE_LIMITED
    verified = await env.service.verify(account["id"])

    assert verified["status"] == STATUS_RATE_LIMITED
    assert verified["active"] is False
    assert verified["last_verified_at"] is not None


async def test_a_browser_outage_is_reported_and_never_faked_as_success(env) -> None:
    account = await env.service.add("chatgpt", label="personal")
    env.browser.reported_status = STATUS_VERIFIED
    await env.service.verify(account["id"])
    assert (await env.service.get(account["id"]))["status"] == STATUS_VERIFIED

    env.browser.offline_for = {"check_account"}
    with pytest.raises(AccountError) as caught:
        await env.service.verify(account["id"])

    assert caught.value.code == "verify_failed"
    stored = await env.service.get(account["id"])
    #: The last honest observation stays, plus the real failure text.
    assert stored["status"] == STATUS_VERIFIED
    assert "not reachable" in (stored["last_error"] or "")


async def test_unknown_provider_is_refused_before_any_profile_is_made(env) -> None:
    with pytest.raises(AccountError) as caught:
        await env.service.add("not-a-provider", label="ghost")
    assert caught.value.code == "unknown_provider"
    assert "create_profile" not in env.browser.calls


async def test_removal_is_destructive_and_needs_confirmation(env) -> None:
    account = await env.service.add("chatgpt", label="personal")

    with pytest.raises(AccountError) as caught:
        await env.service.remove(account["id"], confirm=False)
    assert caught.value.code == "confirmation_required"
    assert "delete_profile" not in env.browser.calls

    result = await env.service.remove(account["id"], confirm=True)
    assert result["profile_deleted"] is True
    assert account["profile_id"] not in env.browser.profiles
    assert await env.service.list(provider_id="chatgpt") == []


async def test_removal_keeps_the_row_when_the_runtime_refuses_to_delete(env) -> None:
    account = await env.service.add("chatgpt", label="personal")
    env.browser.offline_for = {"delete_profile"}

    with pytest.raises(AccountError) as caught:
        await env.service.remove(account["id"], confirm=True)

    assert caught.value.code == "profile_delete_failed"
    assert [item["id"] for item in await env.service.list(provider_id="chatgpt")] == [
        account["id"]
    ]


async def test_provider_summary_counts_only_what_the_service_can_see(env) -> None:
    await env.service.add("chatgpt", label="one")
    second = await env.service.add("chatgpt", label="two")
    env.browser.reported_status = STATUS_VERIFIED
    await env.service.verify(second["id"])
    await env.service.set_active(second["id"])

    summary = await env.service.provider_summary()

    assert summary["connected"] is True
    chatgpt = next(item for item in summary["providers"] if item["id"] == "chatgpt")
    assert len(chatgpt["accounts"]) == 2
    assert chatgpt["configured"] == 2
    assert chatgpt["authenticated"] == 1
    assert chatgpt["active_account"]["label"] == "two"
    assert summary["totals"]["accounts"] == 2
    assert summary["totals"]["authenticated"] == 1
    assert summary["totals"]["providers_with_accounts"] == 1


async def test_events_record_the_real_transitions(env) -> None:
    account = await env.service.add("chatgpt", label="personal")
    await env.service.begin_login(account["id"])

    types = [event["type"] for event in await env.events.history(200)]

    assert "account.added" in types
    assert "account.login_opened" in types
