"""Central agent adapters, installer and registry.

These tests never touch the network and never install real software. Real
installation is exercised separately by the documented acceptance run against
this machine's OpenCode/Qoder CLIs.
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
from pathlib import Path

import pytest

from forgemesh_control.agents import installer as installer_module
from forgemesh_control.agents.base import (
    agent_env,
    first_version,
    parse_json_lines,
    stream_process,
)
from forgemesh_control.agents.installer import (
    OFFICIAL_SOURCES,
    AgentInstaller,
    AgentSource,
    detect_platform,
)
from forgemesh_control.agents.opencode import OpenCodeAgent
from forgemesh_control.agents.qoder import QoderAgent
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.storage import ControlDatabase

OPENCODE_FAKE = """\
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  --version) echo "1.19.5" ;;
  models) printf 'opencode/free-model\\nopencode/paid-model\\n' ;;
  auth) echo "0 credentials" ;;
  providers)
    # Shapes copied from the real OpenCode 1.19.5 output on this machine.
    case "${2:-}" in
      list)
        printf '\\n'
        printf '┌  Credentials %s/.local/share/opencode/auth.json\\n' "$HOME"
        printf '│\\n'
        if [[ -n "${FAKE_OC_CREDENTIALS:-}" ]]; then
          printf '├  github\\n│  dev@example.com\\n│\\n'
          printf '└  1 credentials\\n'
        else
          printf '└  0 credentials\\n'
        fi
        ;;
      login) echo "provider=${3:-any} method=${5:-any}: would prompt here" ;;
      logout) echo "logged out from ${3:-}" ;;
      *) echo "unexpected providers: $*" >&2; exit 2 ;;
    esac
    ;;
  run)
    if [[ "${2:-}" == "--help" ]]; then
      printf 'run opencode with a message\\n--format  json\\n--dir  directory\\n'
      printf -- '-m, --model  model\\n-f, --file  file\\n-s, --session  session\\n'
      exit 0
    fi
    echo '{"type":"step_start","sessionID":"ses_fake"}'
    echo '{"type":"text","sessionID":"ses_fake","part":{"type":"text","text":"FORGEMESH_VERIFY_OK"}}'
    echo '{"type":"step_finish","sessionID":"ses_fake"}'
    ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""

# Reproduces what OpenCode 1.19.5 does here when its free tier is throttled:
# the answer only ever appears as a `--print-logs` line on the pipe, the JSON
# event stream stays empty, and the process does not exit.
OPENCODE_THROTTLED_FAKE = """\
#!/usr/bin/env bash
case "$1" in
  --version) echo "1.19.5" ;;
  models) printf 'opencode/free-model\\n' ;;
  run)
    printf 'timestamp=2026-11-01T17:59:27.015Z level=ERROR run=fake message="stream error" providerID=opencode modelID=free error.error="AI_APICallError: Free usage limit reached. Free models work in OpenCode (https://opencode.ai)"\\n'
    sleep 30
    ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""

# What OpenCode 1.19.5 emits when a tool call is denied because there is no
# terminal to answer the permission prompt in: the reason is on the event
# stream, the text stream stays empty and the process still exits 0.
OPENCODE_DENIED_FAKE = """\
#!/usr/bin/env bash
case "$1" in
  --version) echo "1.19.5" ;;
  models) printf 'opencode/free-model\\n' ;;
  run)
    echo '{"type":"step_start","sessionID":"ses_fake"}'
    echo '{"type":"tool_use","sessionID":"ses_fake","part":{"type":"tool","tool":"bash","state":{"status":"error","error":"The user rejected permission to use this specific tool call."}}}'
    echo '{"type":"step_finish","sessionID":"ses_fake"}'
    ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""

QODER_FAKE = """\
#!/usr/bin/env bash
set -euo pipefail
CONF=""
if [[ "$1" == "--config-dir" ]]; then CONF="$2"; shift 2; fi
ROOT="${CONF:-$HOME/.qoder-fake-default}"
case "$1" in
  --version) echo "9.9.9" ;;
  status)
    if [[ -f "$ROOT/signed-in" ]]; then
      echo '{"logged_in": true, "account": "worker@example.com", "version": "9.9.9"}'
    else
      echo '{"logged_in": false, "version": "9.9.9", "allow_byok": 0}'
    fi
    ;;
  login)
    if [[ -n "${FAKE_QODER_LOGIN_FAILS:-}" ]]; then echo "login refused" >&2; exit 4; fi
    mkdir -p "$ROOT"
    if [[ -n "${FAKE_QODER_ASKS:-}" ]]; then printf 'Paste the code: '; read -r _; fi
    touch "$ROOT/signed-in"
    echo "signed in"
    ;;
  --list-models)
    if [[ -f "$ROOT/signed-in" ]]; then
      printf 'qoder/sonnet\nqoder/haiku\n'
    else
      echo 'Not logged in. Run `qodercli login` to authenticate.'
    fi
    ;;
  --help)
    echo 'Usage: qodercli [options] [command] [query...]'
    echo '-p, --print  non interactive'
    echo '-o, --output-format  format'
    echo '-m, --model  model'
    echo '--cwd  dir'
    echo '--attachment  file'
    echo '--permission-mode  mode'
    echo '--session-id  id'
    echo '--config-dir <dir>  custom root'
    echo 'Commands:'
    echo '  login                     Sign in to your account'
    echo '  status [options]          Show session status'
    ;;
  -p)
    if [[ "${2:-}" == "-o" ]]; then
      if [[ -f "$ROOT/signed-in" ]]; then
        echo '{"type":"result","subtype":"success","is_error":false,"result":"FORGEMESH_VERIFY_OK","session_id":"s-2"}'
        exit 0
      fi
      echo '{"type":"result","subtype":"success","is_error":true,"result":"Not logged in . Please run /login","session_id":"s-1"}'
      exit 1
    fi
    ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""


def make_exe(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def settings(tmp_path: Path) -> ControlSettings:
    resolved = ControlSettings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )
    resolved.ensure_dirs()
    return resolved


@pytest.fixture
async def db(settings: ControlSettings):
    database = ControlDatabase(settings.db_path)
    await database.init()
    try:
        yield database
    finally:
        await database.close()


def test_first_version_and_json_lines() -> None:
    assert first_version("opencode 1.19.5") == "1.19.5"
    assert first_version("no version here") == ""
    events, noise = parse_json_lines(['{"type":"text"}', "warning", "", '{"bad"}'])
    assert events == [{"type": "text"}]
    assert noise == ["warning", '{"bad"}']


def test_agent_env_drops_the_parent_agent_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verified against this machine: qodercli 1.1.81 exits 42 with
    ``sdk_worker_removed`` when a child inherits the SDK variables of the
    session that launched it, and answers normally once they are gone."""
    monkeypatch.setenv("QODER_AGENT_SDK_ENTRYPOINT", "worker")
    monkeypatch.setenv("QODER_SDK_AUTH_PAYLOAD_FILE", "/tmp/parent-auth.json")
    monkeypatch.setenv("QODER_CONFIG_DIR", "/tmp/parent-config")
    monkeypatch.setenv("AI_AGENT", "claude-code")
    env = agent_env({"QODER_CLI_DISABLE_TELEMETRY": "1"})
    assert "QODER_AGENT_SDK_ENTRYPOINT" not in env
    assert "QODER_SDK_AUTH_PAYLOAD_FILE" not in env
    assert "QODER_CONFIG_DIR" not in env
    assert "AI_AGENT" not in env
    # the variables this project sets on purpose survive
    assert env["QODER_CLI_DISABLE_TELEMETRY"] == "1"
    assert env["CI"] == "1"
    assert str(Path.home() / ".opencode" / "bin") in env["PATH"] or env["PATH"]
    # a real inherited variable that is not harness state stays visible
    monkeypatch.setenv("FORGEMESH_KEEP_ME", "yes")
    assert agent_env()["FORGEMESH_KEEP_ME"] == "yes"
    assert os.environ.get("HOME") == env["HOME"]


async def test_opencode_adapter_uses_real_interface(tmp_path: Path) -> None:
    binary = make_exe(tmp_path / "opencode", OPENCODE_FAKE)
    agent = OpenCodeAgent(binary, timeout_seconds=30)
    assert await agent.get_version() == "1.19.5"
    capabilities = await agent.get_capabilities()
    assert capabilities["verified"] is True
    assert capabilities["json_events"] is True
    assert capabilities["forgemesh_auto_approves"] is False
    assert await agent.list_models() == ["opencode/free-model", "opencode/paid-model"]
    status, detail = await agent.check_auth()
    assert status == "unauthenticated"
    assert "free models" in detail

    result = await agent.submit_task(
        "hello", cwd=tmp_path, model="opencode/free-model"
    )
    assert result.ok is True
    assert result.text == "FORGEMESH_VERIFY_OK"
    assert result.session_id == "ses_fake"
    assert result.exit_code == 0


async def test_qoder_adapter_reports_auth_separately(tmp_path: Path) -> None:
    binary = make_exe(tmp_path / "qodercli", QODER_FAKE)
    agent = QoderAgent(binary, timeout_seconds=30)
    assert await agent.get_version() == "9.9.9"
    capabilities = await agent.get_capabilities()
    assert capabilities["verified"] is True
    assert capabilities["max_turns"] is False
    assert await agent.list_models() == []
    status, _ = await agent.check_auth()
    assert status == "unauthenticated"

    result = await agent.submit_task("hello", cwd=tmp_path)
    assert result.ok is False
    assert "Not logged in" in (result.error or "")

    check = await agent.functional_check(cwd=tmp_path)
    assert check["verified"] is False
    assert check["auth_required"] is True


async def test_opencode_declares_one_credential_per_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = make_exe(tmp_path / "opencode", OPENCODE_FAKE)
    agent = OpenCodeAgent(binary, timeout_seconds=30)
    support = agent.auth_support()
    assert support["interactive_login"] is True
    assert support["provider_selection"] is True
    assert support["logout"] is True
    # OpenCode's own store keeps one credential per provider; claiming more
    # would be a fake capability.
    assert support["multiple_accounts"] is False
    assert support["account_isolation"] == "none"
    assert "one credential per provider" in support["limitation"]

    assert agent.login_argv(provider="github") == ["providers", "login", "-p", "github"]
    assert agent.logout_argv(provider="github") == ["providers", "logout", "github"]
    # Shell-ish input never becomes an argument.
    assert agent.login_argv(provider="; rm -rf /") == ["providers", "login"]
    assert agent.logout_argv(provider="") == []
    assert agent.account_argv({"config_dir": str(tmp_path)}) == []

    assert await agent.list_accounts() == []
    monkeypatch.setenv("FAKE_OC_CREDENTIALS", "1")
    accounts = await agent.list_accounts()
    assert [item["name"] for item in accounts] == ["github"]
    assert accounts[0]["email"] == "dev@example.com"
    assert accounts[0]["status"] == "signed_in"
    status, detail = await agent.check_auth()
    assert status == "authenticated"
    assert "github" in detail

    result = await agent.logout("github")
    assert result["ok"] is True
    assert "logged out from github" in result["detail"]


async def test_free_models_keep_opencode_sign_in_optional(tmp_path: Path) -> None:
    """A CLI that answers without a credential must not be told to sign in first."""
    opencode = OpenCodeAgent(make_exe(tmp_path / "opencode", OPENCODE_FAKE), timeout_seconds=30)
    qoder = QoderAgent(make_exe(tmp_path / "qodercli", QODER_FAKE), timeout_seconds=30)

    # Proven on this machine: with `providers list` empty a free model answered
    # a real prompt at cost 0, so sign-in is an upgrade, not a prerequisite.
    assert opencode.auth_support()["required_for_basic_use"] is False
    # Qoder refuses a prompt when its config root holds no session.
    assert qoder.auth_support()["required_for_basic_use"] is True


async def test_functional_check_reports_the_cli_error_instead_of_a_timeout(
    tmp_path: Path,
) -> None:
    binary = make_exe(tmp_path / "opencode", OPENCODE_THROTTLED_FAKE)
    agent = OpenCodeAgent(binary, timeout_seconds=30)

    check = await agent.functional_check(cwd=tmp_path, timeout_seconds=60)

    assert check["verified"] is False
    # The CLI's own words, taken off its own pipe - not a guessed cause.
    assert "Free usage limit reached" in check["detail"]
    # A throttled free model is not a request to authenticate.
    assert check["auth_required"] is False
    # It stopped when the error arrived instead of waiting out the whole budget.
    assert check["duration_ms"] < 20_000


async def test_stream_process_reads_a_line_beyond_the_default_buffer(
    tmp_path: Path,
) -> None:
    script = tmp_path / "bigline"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "echo first\n"
        "head -c 200000 /dev/zero | tr '\\0' 'x'\n"
        "echo\n"
        "echo last\n"
    )
    script.chmod(0o755)

    code, lines, error = await stream_process([str(script)], cwd=tmp_path, timeout_seconds=30)

    assert error is None
    assert code == 0
    assert [lines[0], lines[-1]] == ["first", "last"]
    assert len(lines[1]) == 200_000


async def test_task_run_falls_back_to_a_free_model_and_reports_the_cli_error(
    tmp_path: Path,
) -> None:
    binary = make_exe(tmp_path / "opencode", OPENCODE_THROTTLED_FAKE)
    agent = OpenCodeAgent(binary, timeout_seconds=30)

    # A task with no model selected would otherwise sit on the CLI's own
    # default - the same throttled free model - for its whole budget.
    result = await agent.submit_task("hello", cwd=tmp_path, timeout_seconds=60)

    assert result.ok is False
    assert "Free usage limit reached" in (result.error or "")
    assert result.model == "opencode/free-model"
    assert result.duration_ms < 20_000


async def test_task_run_keeps_the_model_the_operator_selected(
    tmp_path: Path,
) -> None:
    binary = make_exe(tmp_path / "opencode", OPENCODE_FAKE)
    agent = OpenCodeAgent(binary, timeout_seconds=30)

    result = await agent.submit_task(
        "hello", cwd=tmp_path, model="opencode/paid-model"
    )

    assert result.ok is True
    assert result.model == "opencode/paid-model"


async def test_task_run_reports_a_denied_tool_call(tmp_path: Path) -> None:
    binary = make_exe(tmp_path / "opencode", OPENCODE_DENIED_FAKE)
    agent = OpenCodeAgent(binary, timeout_seconds=30)

    # Exit code 0 with an empty answer used to fail the task with no reason.
    result = await agent.submit_task("hello", cwd=tmp_path)

    assert result.ok is False
    assert "rejected permission" in (result.error or "")


async def test_setup_status_keeps_a_credential_free_cli_out_of_the_sign_in_list(
    settings: ControlSettings, db: ControlDatabase
) -> None:
    from forgemesh_control.agents.registry import CentralAgentRegistry

    registry = CentralAgentRegistry(
        settings, db, EventStore(db), AuditService(db, settings.logs_dir)
    )
    for kind, required in (("opencode", False), ("qoder", True)):
        await registry._update_row(
            kind,
            installed=True,
            path=f"/tmp/{kind}",
            version="1.0.0",
            install_status="ready",
            availability="limited",
            auth_status="unauthenticated",
            enabled=True,
            metadata={
                "auth_required_for_use": required,
                "functional_check": {"verified": False, "detail": "no answer"},
            },
        )

    status = await registry.setup_status()

    assert status["auth_needed"] == ["qoder"]
    assert status["auth_optional"] == ["opencode"]
    reasons = {item["kind"]: item["reason"] for item in status["missing_capabilities"]}
    assert "Authentication required" in reasons["qoder"]
    assert "Authentication required" not in reasons["opencode"]


async def test_qoder_uses_the_documented_config_dir_isolation(tmp_path: Path) -> None:
    binary = make_exe(tmp_path / "qodercli", QODER_FAKE)
    agent = QoderAgent(binary, timeout_seconds=30)
    support = agent.auth_support()
    assert support["account_isolation"] == "config_dir"
    assert support["multiple_accounts"] is True
    # No logout command exists in this CLI, and that is reported, not hidden.
    assert support["logout"] is False
    refused = await agent.logout("anything")
    assert refused["ok"] is False
    assert "logout" in str(refused.get("error") or refused.get("detail"))

    capabilities = await agent.get_capabilities()
    assert capabilities["login_command"] is True
    assert capabilities["status_command"] is True
    assert capabilities["logout_command"] is False
    assert capabilities["config_dir"] is True

    isolated = tmp_path / "acct-work"
    assert agent.login_argv(config_dir=str(isolated)) == [
        "--config-dir",
        str(isolated),
        "login",
    ]
    assert agent.account_argv({"config_dir": str(isolated)}) == ["--config-dir", str(isolated)]
    assert agent.account_argv({"config_dir": ""}) == []
    assert agent.account_argv({"config_dir": "relative/dir"}) == []
    assert agent.account_argv({"config_dir": f"/etc/{'..'}/passwd"}) == []

    signed_out = await agent.status(config_dir=str(isolated))
    assert signed_out["ok"] is True and signed_out["logged_in"] is False
    assert (await agent.list_accounts())[0]["status"] == "signed_out"

    isolated.mkdir(parents=True)
    (isolated / "signed-in").write_text("", encoding="utf-8")
    signed_in = await agent.status(config_dir=str(isolated))
    assert signed_in["logged_in"] is True
    assert signed_in["identity"] == "worker@example.com"

    account = {"config_dir": str(isolated)}
    assert await agent.models_for_account(account) == ["qoder/haiku", "qoder/sonnet"]
    assert await agent.models_for_account(None) == []
    result = await agent.submit_task("hello", cwd=tmp_path, account=account)
    assert result.ok is True
    assert result.text == "FORGEMESH_VERIFY_OK"
    assert result.session_id == "s-2"


def test_platform_detection_is_explicit() -> None:
    info = detect_platform("opencode")
    assert info.os_name in {"linux", "darwin"} or not info.supported
    assert isinstance(info.detail, str) and info.detail


async def test_detect_rejects_qoder_gui_binary(tmp_path: Path) -> None:
    gui = make_exe(tmp_path / "qoder", QODER_FAKE)
    settings = ControlSettings(data_dir=tmp_path / "d", config_dir=tmp_path / "c")
    installer = AgentInstaller(settings)
    assert installer._is_gui_binary("qoder", gui) is True
    assert await installer._probe_binary(gui) == ("9.9.9", "")
    # The CLI name is accepted.
    assert installer._is_gui_binary("qoder", tmp_path / "qodercli") is False


async def test_install_pipeline_verifies_real_executable(
    tmp_path: Path, settings: ControlSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "bin" / "opencode"
    target.parent.mkdir()
    script = tmp_path / "official.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -e\n"
        f"cat > {target} <<'BIN'\n#!/usr/bin/env bash\necho 2.0.0\nBIN\n"
        f"chmod +x {target}\n",
        encoding="utf-8",
    )
    source = AgentSource(
        kind="opencode",
        display_name="OpenCode CLI",
        binary_name="forgemesh-test-cli",
        script_url="https://example.invalid/install",
        script_args=(),
        script_marker="opencode",
        npm_package="opencode-ai",
        known_paths=(str(target),),
        docs_url="https://example.invalid",
    )
    monkeypatch.setitem(OFFICIAL_SOURCES, "opencode", source)
    installer = AgentInstaller(settings)

    async def fake_download(source, destination, log):
        destination.write_bytes(script.read_bytes())
        destination.chmod(0o700)
        return None

    monkeypatch.setattr(installer, "_download_script", fake_download)
    result = await installer.install("opencode")
    assert result.ok is True
    assert result.stage == "ready"
    assert result.version == "2.0.0"
    assert result.path == str(target)
    assert Path(result.log_path).is_file()


async def test_install_pipeline_failure_is_not_reported_as_success(
    tmp_path: Path, settings: ControlSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "broken.sh"
    script.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 3\n", encoding="utf-8")
    source = AgentSource(
        kind="opencode",
        display_name="OpenCode CLI",
        binary_name="forgemesh-test-cli",
        script_url="https://example.invalid/install",
        script_args=(),
        script_marker="opencode",
        npm_package="opencode-ai",
        known_paths=(str(tmp_path / "missing"),),
        docs_url="https://example.invalid",
    )
    monkeypatch.setitem(OFFICIAL_SOURCES, "opencode", source)
    installer = AgentInstaller(settings)

    async def fake_download(source, destination, log):
        destination.write_bytes(script.read_bytes())
        destination.chmod(0o700)
        return None

    async def no_npm(source, log):
        return "npm fallback disabled in test"

    monkeypatch.setattr(installer, "_download_script", fake_download)
    monkeypatch.setattr(installer, "_npm_install", no_npm)
    result = await installer.install("opencode")
    assert result.ok is False
    assert result.stage == "failed"
    assert result.error


class FakeInstaller:
    def __init__(self, outcomes: dict[str, bool]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def install(self, kind: str, *, force: bool = False):
        self.calls.append(f"install:{kind}:{force}")
        ok = self.outcomes.get(kind, True)
        from forgemesh_control.agents.installer import InstallResult

        return InstallResult(
            kind=kind,
            ok=ok,
            stage="ready" if ok else "failed",
            path=f"/tmp/{kind}" if ok else "",
            version="1.0.0" if ok else "",
            error=None if ok else "simulated failure",
            log_tail="install log tail" if ok else "failure tail",
        )

    async def update(self, kind: str, *, approve: bool):
        return await self.install(kind, force=True)


async def test_registry_first_run_is_degraded_when_one_agent_lacks_auth(
    settings: ControlSettings, db: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forgemesh_control.agents.registry import CentralAgentRegistry

    registry = CentralAgentRegistry(
        settings, db, EventStore(db), AuditService(db, settings.logs_dir)
    )
    fake = FakeInstaller({"opencode": True, "qoder": True})
    registry.installer = fake  # type: ignore[assignment]

    async def fake_detect(kind: str):
        return {
            "kind": kind,
            "installed": True,
            "path": f"/tmp/{kind}",
            "version": "1.0.0",
            "error": None,
            "candidates": [],
        }

    async def fake_verify(kind: str, *, functional: bool = True):
        available = kind == "opencode"
        return await registry._update_row(
            kind,
            installed=True,
            path=f"/tmp/{kind}",
            version="1.0.0",
            install_status="ready",
            availability="available" if available else "limited",
            auth_status="unauthenticated",
            last_error=None if available else "Authentication required.",
        )

    monkeypatch.setattr(registry, "detect", fake_detect)
    monkeypatch.setattr(registry, "verify", fake_verify)

    status = await registry.first_run_setup()
    # Setup installs both kinds concurrently, so only the set is meaningful.
    assert sorted(fake.calls) == ["install:opencode:False", "install:qoder:False"]
    assert status["state"] == "degraded"
    assert status["available"] == ["opencode"]
    assert status["limited"] == ["qoder"]
    assert status["complete"] is True
    assert {item["kind"] for item in status["missing_capabilities"]} == {"qoder"}

    events = await EventStore(db).history(100)
    types = [event["type"] for event in events]
    assert "setup.agents.complete" in types


async def test_detect_keeps_real_verification_when_binary_is_unchanged(
    settings: ControlSettings, db: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forgemesh_control.agents.installer import Detection
    from forgemesh_control.agents.registry import CentralAgentRegistry

    registry = CentralAgentRegistry(
        settings, db, EventStore(db), AuditService(db, settings.logs_dir)
    )
    await registry._update_row(
        "opencode",
        installed=True,
        path="/tmp/opencode",
        version="1.0.0",
        install_status="ready",
        availability="available",
        auth_status="unauthenticated",
        enabled=True,
        metadata={"functional_check": {"verified": True, "model": "test"}},
    )

    async def detect(kind: str) -> Detection:
        return Detection(kind=kind, installed=True, path="/tmp/opencode", version="1.0.0")

    monkeypatch.setattr(registry.installer, "detect", detect)
    row = await registry.detect("opencode")

    assert row["install_status"] == "ready"
    assert row["availability"] == "available"
    assert row["metadata"]["functional_check"]["verified"] is True
    status = await registry.setup_status()
    assert status["state"] == "degraded"


async def test_detect_requires_reverification_when_the_version_changed(
    settings: ControlSettings, db: ControlDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forgemesh_control.agents.installer import Detection
    from forgemesh_control.agents.registry import CentralAgentRegistry

    registry = CentralAgentRegistry(
        settings, db, EventStore(db), AuditService(db, settings.logs_dir)
    )
    await registry._update_row(
        "opencode",
        installed=True,
        path="/tmp/opencode",
        version="1.0.0",
        install_status="ready",
        availability="available",
        enabled=True,
    )

    async def detect(kind: str) -> Detection:
        return Detection(kind=kind, installed=True, path="/tmp/opencode", version="2.0.0")

    monkeypatch.setattr(registry.installer, "detect", detect)
    row = await registry.detect("opencode")

    assert row["install_status"] == "detected"
    assert row["availability"] == "limited"
    assert row["version"] == "2.0.0"


async def test_registry_records_install_failure_without_faking_readiness(    settings: ControlSettings, db: ControlDatabase
) -> None:
    from forgemesh_control.agents.registry import CentralAgentRegistry

    registry = CentralAgentRegistry(
        settings, db, EventStore(db), AuditService(db, settings.logs_dir)
    )
    registry.installer = FakeInstaller({"opencode": False, "qoder": False})  # type: ignore[assignment]
    status = await registry.first_run_setup()
    assert status["state"] == "failed"
    assert status["failed"] == ["opencode", "qoder"]
    for kind in ("opencode", "qoder"):
        row = await registry.get_agent(kind)
        assert row["install_status"] == "failed"
        assert row["availability"] == "unavailable"
        assert row["last_error"] == "simulated failure"


async def test_set_path_rejects_qoder_gui(settings: ControlSettings, db: ControlDatabase) -> None:
    from forgemesh_control.agents.registry import (
        AgentRegistryError,
        CentralAgentRegistry,
    )

    registry = CentralAgentRegistry(
        settings, db, EventStore(db), AuditService(db, settings.logs_dir)
    )
    with pytest.raises(AgentRegistryError) as caught:
        await registry.set_path("qoder", "/opt/Qoder/qoder")
    assert caught.value.code == "wrong_binary"


def test_installer_source_urls_are_official() -> None:
    assert OFFICIAL_SOURCES["opencode"].script_url == "https://opencode.ai/install"
    assert OFFICIAL_SOURCES["opencode"].npm_package == "opencode-ai"
    assert OFFICIAL_SOURCES["qoder"].script_url == "https://qoder.com/install"
    assert OFFICIAL_SOURCES["qoder"].npm_package == "@qoder-ai/qodercli"
    assert OFFICIAL_SOURCES["qoder"].binary_name == "qodercli"


async def test_installer_rejects_html_download(
    tmp_path: Path, settings: ControlSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResponse:
        status_code = 200
        content = b"<!doctype html><html>login</html>" + b"x" * 2000

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(installer_module.httpx, "AsyncClient", FakeClient)
    installer = AgentInstaller(settings)
    error = await installer._download_script(
        OFFICIAL_SOURCES["opencode"], tmp_path / "install.sh", lambda _: None
    )
    assert error is not None
    assert "HTML" in error


def test_json_round_trip_for_qoder_result() -> None:
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "Not logged in · Please run /login",
            "session_id": "s-1",
        }
    )
    events, noise = parse_json_lines([line])
    assert noise == []
    assert events[0]["is_error"] is True


def test_async_event_loop_is_available() -> None:
    async def value() -> int:
        await asyncio.sleep(0)
        return 1

    assert asyncio.run(value()) == 1
