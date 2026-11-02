"""Shared contract for CLI coding-agent adapters.

Every adapter implements the same operations, but capabilities are declared
explicitly and marked unverified until a real functional check runs.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import os
import re
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LineHandler = Callable[[str], Awaitable[None] | None]

#: One CLI event is one line, and a single tool result can be bigger than
#: asyncio's 64 KiB read buffer, which would abort a healthy run.
STREAM_LINE_LIMIT_BYTES = 32 * 1024 * 1024

INSTALL_STATUSES = {
    "not_detected",
    "detected",
    "installing",
    "verifying",
    "ready",
    "degraded",
    "failed",
    "disabled",
}

STAGES = (
    "not_detected",
    "already_installed",
    "downloading",
    "installing",
    "configuring",
    "verifying",
    "ready",
    "failed",
)


@dataclass(slots=True)
class AgentInfo:
    kind: str
    display_name: str
    installed: bool = False
    path: str = ""
    version: str = ""
    install_status: str = "not_detected"
    configuration_status: str = "unconfigured"
    auth_status: str = "unknown"
    availability: str = "unavailable"
    os_supported: bool = True
    last_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "installed": self.installed,
            "path": self.path,
            "version": self.version,
            "install_status": self.install_status,
            "configuration_status": self.configuration_status,
            "auth_status": self.auth_status,
            "availability": self.availability,
            "os_supported": self.os_supported,
            "last_error": self.last_error,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class AgentRunResult:
    ok: bool
    text: str
    exit_code: int | None
    duration_ms: int
    session_id: str | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    #: The model the run actually used, which can differ from the requested one
    #: when the agent had to fall back. Recorded so the task log can say so.
    model: str | None = None


class CodingAgent(abc.ABC):
    """One CLI coding agent on the central server."""

    kind: str
    display_name: str

    def __init__(self, binary: Path, *, timeout_seconds: int) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds

    # ---------------------------------------------------------------- discovery
    @abc.abstractmethod
    async def get_version(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_models(self) -> list[str]:
        raise NotImplementedError

    @abc.abstractmethod
    async def check_auth(self) -> tuple[str, str]:
        """Return ``(status, detail)``; never returns a credential."""
        raise NotImplementedError

    # ------------------------------------------------------------ authentication
    def auth_support(self) -> dict[str, Any]:
        """What this CLI can really do about credentials.

        Defaults report "unsupported" so the dashboard never offers a button
        that the tool cannot honour. ``required_for_basic_use`` is False only
        for a tool the vendor documents as runnable without any credential, so
        setup never tells the user to sign in before they can use it.
        """
        return {
            "interactive_login": False,
            "provider_selection": False,
            "logout": False,
            "multiple_accounts": False,
            "account_isolation": "none",
            "required_for_basic_use": True,
            "limitation": "This adapter declares no authentication interface.",
        }

    def login_argv(self, *, provider: str = "", config_dir: str = "") -> list[str]:
        """The documented interactive sign-in command.

        Only commands the real CLI advertises may appear here. An empty list
        means the tool has no supported interactive sign-in.
        """
        return []

    def logout_argv(self, *, provider: str = "") -> list[str]:
        """The documented sign-out command, or an empty list."""
        return []

    async def logout(self, provider: str) -> dict[str, Any]:
        """Run the CLI's own sign-out. Unsupported tools say so plainly."""
        return {
            "ok": False,
            "error": self.auth_support().get("limitation")
            or "This CLI documents no logout command.",
        }

    async def list_accounts(self) -> list[dict[str, Any]]:
        """Accounts reported by the CLI itself, never invented locally."""
        return []

    def account_argv(self, account: dict[str, Any] | None) -> list[str]:
        """Extra flags that make a run use one account's own credentials."""
        return []

    async def models_for_account(self, account: dict[str, Any] | None) -> list[str]:
        """Models reachable as one account; falls back to the tool's default."""
        return await self.list_models()

    # ----------------------------------------------------------------- execution
    @abc.abstractmethod
    async def submit_task(
        self,
        prompt: str,
        *,
        cwd: Path,
        model: str | None = None,
        files: list[Path] | None = None,
        on_line: LineHandler | None = None,
        timeout_seconds: int | None = None,
        account: dict[str, Any] | None = None,
    ) -> AgentRunResult:
        raise NotImplementedError

    async def health_check(self) -> dict[str, Any]:
        version = await self.get_version()
        return {
            "kind": self.kind,
            "binary": str(self.binary),
            "version": version,
            "ok": bool(version),
        }


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the child's own process group so its helpers die with it.

    The child is started in a new session, so this never reaches the caller's
    group. Without it an aborted CLI can leave a helper holding the output pipe,
    and asyncio only finishes ``process.wait()`` once that pipe is closed - a
    three-second abort turned into the helper's full runtime.
    """
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(sig)


async def stream_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: int,
    on_line: LineHandler | None = None,
    early_exit: Callable[[str], bool] | None = None,
) -> tuple[int, list[str], str | None]:
    """Run a process, streaming both pipes line by line with a hard timeout.

    ``early_exit`` ends the run as soon as one streamed line says the answer
    will never arrive, instead of burning the whole timeout on a process that
    is waiting for nothing.

    The read buffer is widened because a CLI writes one JSON event per line and
    a single tool result can exceed asyncio's 64 KiB default, which would abort
    the whole run with ``Separator is found, but chunk is longer than limit``.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=STREAM_LINE_LIMIT_BYTES,
        )
    except FileNotFoundError as exc:
        return 127, [], f"Executable not found: {argv[0]} ({exc})"
    except PermissionError as exc:
        return 126, [], f"Executable is not runnable: {argv[0]} ({exc})"

    lines: list[str] = []

    async def stop() -> None:
        """Terminate, then force, once the CLI has already stated its error.

        ``returncode`` is polled rather than awaited: a second concurrent
        ``wait()`` on the same process cancels the one the caller's gather is
        waiting on.
        """
        _signal_group(process, signal.SIGTERM)
        for _ in range(3):
            await asyncio.sleep(0.5)
            if process.returncode is not None:
                break
        _signal_group(process, signal.SIGKILL)

    async def pump() -> None:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            lines.append(line)
            if on_line is not None:
                result = on_line(line)
                if asyncio.iscoroutine(result):
                    await result
            if len(lines) > 20_000:
                del lines[: len(lines) - 20_000]
            if early_exit is not None and early_exit(line):
                await stop()
                break

    try:
        await asyncio.wait_for(
            asyncio.gather(pump(), process.wait()), timeout=timeout_seconds
        )
    except TimeoutError:
        _signal_group(process, signal.SIGKILL)
        with_suppress = asyncio.gather(process.wait(), return_exceptions=True)
        await with_suppress
        return -1, lines, f"Timed out after {timeout_seconds}s"
    stdout_left = process.stdout
    if stdout_left is not None:
        stdout_left.feed_eof()
    return process.returncode or 0, lines, None


_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
BOX_CHARS = "│┌┐└┘├┤─┬┴┼╭╮╰╯╌╍▌▐"


def strip_ansi(text: str) -> str:
    """Remove terminal control sequences so captured output can be shown as text."""
    return _ANSI_RE.sub("", text or "")


def first_version(text: str) -> str:
    """Extract the first semantic version from command output, if any."""
    match = _VERSION_RE.search(text or "")
    return match.group(0) if match else ""


#: Variables a parent agent session exports for its own CLI child. Inherited by
#: a nested run they are wrong twice over: ``qodercli`` answers exit 42 with
#: ``sdk_worker_removed`` because it thinks the control server is its SDK host,
#: and the config root points at the parent's session instead of this account.
_HARNESS_ENV_NAMES = frozenset(
    {
        "AGENT",
        "AI_AGENT",
        "CHROME_DESKTOP",
        "ELECTRON_PA_APP_NAME",
        "QODER_CLI",
        "QODER_CONFIG_DIR",
        "QODERCN_CONFIG_DIR",
        "QODER_PID",
    }
)
_HARNESS_ENV_PREFIXES = (
    "QODER_AGENT_SDK_",
    "QODERCLI_",
    "QODER_FEATURE_",
    "QODER_MODEL_",
    "QODER_PRODUCT_",
    "QODER_SEARCH_",
    "QODER_SDK_",
    "QODER_SECURITY_SCAN_",
    "QODER_SESSION_",
    "QODER_SITES_",
    "QODER_WORKER_",
    "__QODER_",
)


def _is_harness_var(name: str) -> bool:
    return name in _HARNESS_ENV_NAMES or name.startswith(_HARNESS_ENV_PREFIXES)


def agent_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A predictable environment for CLIs installed under the user's home.

    The desktop app is often launched from a session without the shell's
    custom PATH, so the known per-user bin directories are prepended.
    """
    env = {key: value for key, value in os.environ.items() if not _is_harness_var(key)}
    home = Path(env.get("HOME", str(Path.home())))
    preferred = [
        home / ".local" / "bin",
        home / ".opencode" / "bin",
        home / "npm-global" / "bin",
        home / ".npm-global" / "bin",
    ]
    existing = env.get("PATH", "")
    prefix = ":".join(str(entry) for entry in preferred if entry.is_dir())
    env["PATH"] = f"{prefix}:{existing}" if prefix else existing
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    if extra:
        env.update(extra)
    return env


def parse_json_lines(lines: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Split stdout into JSON events and unparseable noise."""
    import json

    events: list[dict[str, Any]] = []
    noise: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                noise.append(stripped)
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
            else:
                noise.append(stripped)
        else:
            noise.append(stripped)
    return events, noise
