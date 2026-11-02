"""GUI authentication service for the central coding CLIs.

Mocked in the sense that no network, no real provider account and no real
credential is involved: the CLI is a local bash stand-in that reproduces the
command shapes verified on this machine (OpenCode 1.19.5, Qoder CLI 1.1.81).
Everything else is real - a real subprocess, a real pseudo-terminal, real
parsing of the fake's real stdout, and the real status command deciding
whether a login succeeded.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_control_agents import OPENCODE_FAKE, QODER_FAKE, make_exe

from forgemesh_control.agent_auth import (
    HAS_PTY,
    AgentAuthError,
    AgentAuthService,
    _escape_complete,
    _trailing_escape,
)
from forgemesh_control.agents.registry import CentralAgentRegistry
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.storage import ControlDatabase


@pytest.fixture
def settings(tmp_path: Path) -> ControlSettings:
    resolved = ControlSettings(data_dir=tmp_path / "data", config_dir=tmp_path / "config")
    resolved.ensure_dirs()
    return resolved


@pytest.fixture
async def env(settings: ControlSettings, tmp_path: Path):
    database = ControlDatabase(settings.db_path)
    await database.init()
    events = EventStore(database)
    audit = AuditService(database, settings.logs_dir)
    registry = CentralAgentRegistry(settings, database, events, audit)
    service = AgentAuthService(settings, database, events, audit, registry)
    binaries = {
        "opencode": make_exe(tmp_path / "opencode", OPENCODE_FAKE),
        "qoder": make_exe(tmp_path / "qodercli", QODER_FAKE),
    }
    for kind, binary in binaries.items():
        await registry._update_row(
            kind,
            installed=True,
            path=str(binary),
            version="1.0.0",
            install_status="ready",
            configuration_status="configured",
            availability="limited",
            metadata={
                "capabilities": {
                    "verified": True,
                    "login_command": True,
                    "status_command": True,
                    "logout_command": False,
                    "config_dir": True,
                }
            },
        )
    try:
        yield SimpleNamespace(
            settings=settings, db=database, events=events, registry=registry,
            service=service, binaries=binaries, tmp_path=tmp_path,
        )
    finally:
        await service.shutdown()
        await database.close()


async def _wait_for(predicate, *, timeout: float = 20.0, what: str = "condition") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{what} never happened")


async def test_capability_states_what_the_cli_can_actually_do(env) -> None:
    opencode = await env.service.capability("opencode")
    assert opencode["installed"] is True
    assert opencode["interactive_login"] is True
    assert opencode["multiple_accounts"] is False
    assert "one credential per provider" in opencode["limitation"]

    qoder = await env.service.capability("qoder")
    assert qoder["multiple_accounts"] is True
    assert qoder["account_isolation"] == "config_dir"
    # This CLI ships no logout, so the dashboard must not offer one.
    assert qoder["logout"] is False
    assert qoder["terminal"] == ("pty" if HAS_PTY else "pipe")


async def test_add_account_is_refused_when_the_cli_cannot_isolate(env) -> None:
    with pytest.raises(AgentAuthError) as caught:
        await env.service.add_account("opencode", "second")
    assert caught.value.code == "unsupported"
    assert "one credential per provider" in caught.value.message
    assert await env.service.list_accounts("opencode") == []


async def test_account_names_are_validated(env) -> None:
    for bad in ("", "A", "x", "has space", "../escape", "a" * 40):
        with pytest.raises(AgentAuthError) as caught:
            await env.service.add_account("qoder", bad)
        assert caught.value.code == "invalid_name", bad


async def test_isolated_accounts_are_verified_by_the_cli_itself(env) -> None:
    created = await env.service.add_account("qoder", "work")
    account = created["account"]
    assert account["config_dir"].endswith("qoder/work")
    assert Path(account["config_dir"]).is_dir()
    assert created["login_argv"][:2] == ["--config-dir", account["config_dir"]]

    accounts = {item["name"]: item for item in await env.service.sync_accounts("qoder")}
    assert set(accounts) >= {"default", "work"}
    assert accounts["work"]["status"] == "signed_out"
    assert accounts["default"]["status"] == "signed_out"
    assert await env.service.active_account("qoder") is None

    # Nothing in ForgeMesh decides that a login worked; only qodercli status does.
    Path(accounts["work"]["config_dir"], "signed-in").write_text("", encoding="utf-8")
    accounts = {item["name"]: item for item in await env.service.sync_accounts("qoder")}
    assert accounts["work"]["status"] == "signed_in"
    assert "worker@example.com" in accounts["work"]["detail"]
    assert accounts["default"]["status"] == "signed_out"

    active = await env.service.active_account("qoder")
    assert active is not None and active["name"] == "work"

    switch = await env.service.add_account("qoder", "personal")
    Path(switch["account"]["config_dir"], "signed-in").write_text("", encoding="utf-8")
    await env.service.sync_accounts("qoder")
    await env.service.set_active("qoder", switch["account"]["id"])
    active = await env.service.active_account("qoder")
    assert active is not None and active["name"] == "personal"
    stored = await env.service.list_accounts("qoder")
    assert sum(1 for item in stored if item["active"]) == 1


async def test_set_active_refused_for_a_cli_without_switching(env) -> None:
    with pytest.raises(AgentAuthError) as caught:
        await env.service.set_active("opencode", 1)
    assert caught.value.code == "unsupported"


async def test_a_signed_out_account_cannot_be_made_active(env) -> None:
    """Activating an account with no credential would only break every run."""
    account = (await env.service.add_account("qoder", "cold"))["account"]
    await env.service.sync_accounts("qoder")
    with pytest.raises(AgentAuthError) as caught:
        await env.service.set_active("qoder", account["id"])
    assert caught.value.code == "not_signed_in"
    assert "not signed in" in caught.value.message

    Path(account["config_dir"], "signed-in").write_text("", encoding="utf-8")
    await env.service.sync_accounts("qoder")
    activated = await env.service.set_active("qoder", account["id"])
    assert activated["active"] is True and activated["status"] == "signed_in"


async def test_removal_needs_confirmation_and_reports_the_limit(env) -> None:
    account = (await env.service.add_account("qoder", "temp"))["account"]
    with pytest.raises(AgentAuthError) as caught:
        await env.service.remove_account("qoder", account["id"], confirm=False)
    assert caught.value.code == "confirmation_required"
    assert await env.service.list_accounts("qoder") != []

    result = await env.service.remove_account("qoder", account["id"], confirm=True)
    assert result["removed"] == "temp"
    # Without --purge the CLI's own session file is left alone; the response says so.
    assert result["credential_removed"] is False
    assert "no logout command" in result["note"]
    assert Path(account["config_dir"]).is_dir()

    other = (await env.service.add_account("qoder", "gone"))["account"]
    Path(other["config_dir"], "marker").write_text("x", encoding="utf-8")
    removed = await env.service.remove_account(
        "qoder", other["id"], confirm=True, purge=True
    )
    assert removed["credential_removed"] is True
    assert not Path(other["config_dir"]).exists()


async def test_removal_outside_forgemesh_roots_is_refused(env) -> None:
    from sqlalchemy import update

    from forgemesh_control.models import AgentAccountRow

    account = (await env.service.add_account("qoder", "safe"))["account"]
    async with env.db.session_scope() as scope:
        await scope.execute(
            update(AgentAccountRow)
            .where(AgentAccountRow.id == account["id"])
            .values(config_dir=str(env.tmp_path))
        )
    with pytest.raises(AgentAuthError) as caught:
        await env.service.remove_account("qoder", account["id"], confirm=True, purge=True)
    assert caught.value.code == "invalid_path"


async def test_logout_is_refused_where_the_cli_has_no_command(env) -> None:
    with pytest.raises(AgentAuthError) as caught:
        await env.service.logout("qoder", "anything")
    assert caught.value.code == "unsupported"
    assert "no logout command" in caught.value.message


@pytest.mark.skipif(not HAS_PTY, reason="interactive terminal auth needs a pty")
async def test_hosted_login_streams_real_output_and_signs_in(env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_QODER_ASKS", "1")
    account = (await env.service.add_account("qoder", "terminal"))["account"]

    view = await env.service.start_login("qoder", account_id=account["id"])
    assert view["status"] == "running"
    assert view["mode"] == "pty"
    assert "--config-dir" in view["command"]
    assert view["id"].startswith("ath_")

    # The dashboard reads deltas: each poll asks for what it has not seen yet.
    cursor = 0
    prompt_seen = view["text"]
    deadline = time.time() + 20
    while time.time() < deadline and "Paste the code" not in prompt_seen:
        page = await env.service.session_view("qoder", view["id"], offset=cursor)
        prompt_seen += page["text"]
        cursor = page["next_offset"]
        await asyncio.sleep(0.05)
    assert "Paste the code" in prompt_seen

    await env.service.send_text("qoder", view["id"], "ABC-123")
    await env.service.send_key("qoder", view["id"], "enter")
    await _wait_for(
        lambda: env.service._session("qoder", view["id"]).status != "running",  # noqa: SLF001
        what="the login process did not finish",
    )
    final = await env.service.session_view("qoder", view["id"], offset=0)
    assert final["status"] == "exited"
    assert final["exit_code"] == 0

    # The server re-reads the CLI's own status after the process exits, so wait
    # for that durable finish event before asserting anything about accounts.
    events: list[str] = []
    deadline = time.time() + 20
    while time.time() < deadline:
        events = [event["type"] for event in await env.events.history(200)]
        if "agent.auth.finished" in events:
            break
        await asyncio.sleep(0.05)
    assert "agent.auth.started" in events
    assert "agent.auth.finished" in events

    accounts = {item["name"]: item for item in await env.service.list_accounts("qoder")}
    assert accounts["terminal"]["status"] == "signed_in"
    history = await env.service.session_history("qoder")
    assert history[0]["id"] == view["id"]
    assert history[0]["status"] == "exited"
    # The live terminal shows what the user typed; durable storage does not.
    assert "ABC-123" in final["text"]
    assert "ABC-123" not in history[0]["tail"]


@pytest.mark.skipif(not HAS_PTY, reason="interactive terminal auth needs a pty")
async def test_default_sign_in_turns_limited_into_a_verified_agent(env, monkeypatch) -> None:
    """A successful sign-in must not leave the agent parked on "limited"."""
    monkeypatch.setenv("HOME", str(env.tmp_path))
    view = await env.service.start_login("qoder")
    assert view["status"] == "running"

    deadline = time.time() + 20
    while time.time() < deadline:
        events = [event["type"] for event in await env.events.history(100)]
        if "agent.auth.finished" in events:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("the sign-in session never finished")

    row: dict = {}
    while time.time() < deadline:
        row = await env.registry.get_agent("qoder")
        if row.get("availability") == "available":
            break
        await asyncio.sleep(0.05)
    assert row.get("availability") == "available", row.get("last_error")
    check = (row.get("metadata") or {}).get("functional_check") or {}
    assert check.get("verified") is True


def test_stored_output_never_keeps_a_credential() -> None:
    """Durable storage is scrubbed; only the live terminal keeps the raw link."""
    from forgemesh_control.agent_auth import redact_output

    raw = (
        "  https://qoder.com/device/selectAccounts?challenge=ABC123xyz&nonce=n-1\n"
        "Paste the code: ABC-1234\n"
        "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456\n"
        "error code: 42\n"
    )
    clean = redact_output(raw)
    assert "ABC123xyz" not in clean and "n-1" not in clean
    assert "ABC-1234" not in clean
    assert "ghp_" not in clean
    # The link itself stays readable so the user still knows which page to open.
    assert "https://qoder.com/device/selectAccounts?" in clean
    assert "error code: 42" in clean


async def test_unknown_account_and_kind_are_reported(env) -> None:
    with pytest.raises(AgentAuthError) as caught:
        await env.service.set_active("qoder", 4242)
    assert caught.value.code == "unknown_account"
    with pytest.raises(AgentAuthError) as missing:
        await env.service.session_view("qoder", "ath_nope")
    assert missing.value.code == "unknown_session"


@pytest.mark.skipif(not HAS_PTY, reason="needs a pty to open a session")
async def test_only_one_login_session_per_agent_at_a_time(env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_QODER_ASKS", "1")
    account = (await env.service.add_account("qoder", "busy"))["account"]
    view = await env.service.start_login("qoder", account_id=account["id"])
    with pytest.raises(AgentAuthError) as caught:
        await env.service.start_login("qoder", account_id=account["id"])
    assert caught.value.code == "session_running"
    await env.service.cancel("qoder", view["id"])
    await _wait_for(
        lambda: env.service._session("qoder", view["id"]).status != "running",  # noqa: SLF001
        what="the cancelled login process kept running",
    )


@pytest.mark.skipif(not HAS_PTY, reason="needs a pty to open a session")
async def test_input_validation_and_closed_sessions(env, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_QODER_ASKS", "1")
    account = (await env.service.add_account("qoder", "keys"))["account"]
    view = await env.service.start_login("qoder", account_id=account["id"])
    with pytest.raises(AgentAuthError) as caught:
        await env.service.send_key("qoder", view["id"], "ctrl_alt_del")
    assert caught.value.code == "invalid_key"
    with pytest.raises(AgentAuthError) as empty:
        await env.service.send_text("qoder", view["id"], "")
    assert empty.value.code == "empty_input"

    await env.service.send_key("qoder", view["id"], "ctrl_u")
    await env.service.send_key("qoder", view["id"], "ctrl_c")
    await _wait_for(
        lambda: env.service._session("qoder", view["id"]).status != "running",  # noqa: SLF001
        what="Ctrl-C did not end the session",
    )
    with pytest.raises(AgentAuthError) as closed:
        await env.service.send_key("qoder", view["id"], "enter")
    assert closed.value.code in {"session_closed", "unknown_session"}


def test_partial_escape_sequences_are_recognised() -> None:
    assert _trailing_escape("abc\x1b[2K") == "\x1b[2K"
    assert _escape_complete("\x1b[2K") is True
    assert _escape_complete("\x1b[2") is False
    assert _escape_complete("\x1b") is False
    assert _trailing_escape("plain text") == ""


async def test_ctrl_c_signals_the_process_group_directly(env) -> None:
    """The ^C byte alone is dropped before the pty has a foreground group."""
    calls: list[str] = []

    class _Session:
        kind = "qoder"
        id = "ath_stub"
        status = "running"

        def write(self, data: bytes) -> None:
            calls.append("write")

        def interrupt(self) -> None:
            calls.append("interrupt")

        def output(self, offset: int) -> dict:
            return {"text": "", "next_offset": offset, "truncated": False}

    env.service._sessions["ath_stub"] = _Session()  # noqa: SLF001
    await env.service.send_key("qoder", "ath_stub", "ctrl_c")
    assert calls == ["write", "interrupt"]
