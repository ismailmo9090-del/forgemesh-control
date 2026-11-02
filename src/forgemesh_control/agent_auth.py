"""GUI-driven authentication and account state for the central coding CLIs.

Why a terminal exists at all: ``opencode providers login`` and ``qodercli login``
are the officially documented sign-in flows and both expect a terminal. Rather
than telling the operator to open one, ForgeMesh hosts the session: it spawns
the CLI's own command on a pseudo-terminal, streams the real output to the
dashboard and forwards exactly the bytes the operator asks to send. Nothing is
typed on the user's behalf, so no credential is captured, stored or guessed by
this service, and a login is only reported as successful after the CLI's own
status command says so.

Availability is stated per platform. ``pty`` is POSIX-only; where it is missing
the same command runs over ordinary pipes and any prompt that genuinely needs a
terminal fails with its own error, which is recorded instead of hidden. Windows
behaviour has never been tested here, so nothing claims it works.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from forgemesh_browser.security.permissions import ensure_private_dir
from forgemesh_control.agents.base import CodingAgent, agent_env, strip_ansi
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.models import AgentAccountRow, AgentAuthSessionRow, iso, utcnow
from forgemesh_control.storage import ControlDatabase

logger = logging.getLogger("forgemesh.control.agent_auth")

try:  # POSIX only; the fallback below is deliberate, not a silent success.
    import fcntl
    import pty
    import termios

    HAS_PTY = hasattr(pty, "openpty")
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]
    pty = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    HAS_PTY = False

MAX_BUFFER_CHARS = 300_000
MAX_TAIL_CHARS = 3_000
AUTH_SESSION_TIMEOUT_SECONDS = 900
#: Closed sessions kept in memory for the dashboard's last poll before only the
#: scrubbed database tail remains.
MAX_MEMORY_SESSIONS = 8
#: How much of a closed session's last screen the history list carries, so the
#: dashboard can explain a failed sign-in without opening the session again.
HISTORY_TAIL_CHARS = 1200
ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
TERMINAL_COLS = (20, 400)
TERMINAL_ROWS = (5, 200)
KEY_SEQUENCES: dict[str, bytes] = {
    "enter": b"\r",
    "tab": b"\t",
    "backspace": b"\x7f",
    "escape": b"\x1b",
    "space": b" ",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "ctrl_c": b"\x03",
    "ctrl_d": b"\x04",
    "ctrl_u": b"\x15",
}
# A trailing escape sequence can straddle a read boundary; it is held back
# rather than being rendered as visible junk.

#: Device-authorization URLs and pasted codes appear in a login session's
#: output. Live deltas stay verbatim - the signed-in user needs the link to
#: finish the flow - but nothing credential-shaped is written to the session
#: row, the event log or the audit trail.
_REDACTED = "[redacted]"
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:challenge|nonce|state|code|token|access_token|refresh_token|"
    r"id_token|secret|client_secret|api[_-]?key|key)=)([^&#\s]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer[ =]?[\"']?)[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNED_RE = re.compile(
    r"(?i)([\"']?(?:password|passwd|passcode|otp|pin|token|secret|api[_-]?key|key|"
    r"verification code|device code|one[- ]time code|code)[\"']?\s*[:=]\s*[\"']?)"
    r"([A-Za-z0-9._~+/=-]{5,})"
)


class AgentAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str = "agent_auth_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def redact_output(text: str) -> str:
    """Strip credential-shaped substrings from output before it is stored."""
    if not text:
        return ""
    redacted = _SECRET_QUERY_RE.sub(lambda m: m.group(1) + _REDACTED, text)
    redacted = _BEARER_RE.sub(lambda m: m.group(1) + _REDACTED, redacted)
    return _ASSIGNED_RE.sub(lambda m: m.group(1) + _REDACTED, redacted)


def _rendered(text: str) -> str:
    """Terminal output as displayable text: no control codes, one line each."""
    return strip_ansi(text).replace("\r\n", "\n").replace("\r", "\n")


class TerminalSession:
    """One running CLI auth command with an append-only output buffer."""

    def __init__(
        self,
        *,
        kind: str,
        argv: list[str],
        cwd: Path,
        on_exit: Callable[[TerminalSession], Any],
    ) -> None:
        self.id = f"ath_{uuid.uuid4().hex[:12]}"
        self.kind = kind
        self.argv = [str(part) for part in argv]
        self.cwd = cwd
        self.on_exit = on_exit
        self.command = " ".join(self.argv)
        self.started_at = utcnow()
        self.ended_at: datetime | None = None
        self.exit_code: int | None = None
        self.status = "starting"
        self.error: str | None = None
        self.mode = "pty" if HAS_PTY else "pipe"
        self.timeout_seconds = AUTH_SESSION_TIMEOUT_SECONDS
        self.loop = asyncio.get_running_loop()
        self._lock = threading.Lock()
        self._text = ""
        self._pending = ""
        self._start_offset = 0
        self._total_chars = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._reader: threading.Thread | None = None
        self._cancelled = False
        self._alarm: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------ startup
    def start(self, *, cols: int, rows: int, timeout_seconds: int) -> None:
        env = agent_env()
        # ``CI=1`` makes some CLIs refuse to prompt at all; an auth session is
        # explicitly interactive, and colours are off so output renders as text.
        env.pop("CI", None)
        env["CI"] = ""
        env["NO_COLOR"] = "1"
        env["TERM"] = "xterm-256color" if HAS_PTY else "dumb"
        env["FORGEMESH_AGENT_AUTH"] = "1"
        self.timeout_seconds = max(30, int(timeout_seconds))
        if HAS_PTY:
            master, slave = pty.openpty()
            try:
                self._set_winsize(slave, cols=cols, rows=rows)
                self._process = subprocess.Popen(
                    self.argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=str(self.cwd),
                    env=env,
                    close_fds=True,
                    start_new_session=True,
                )
            except OSError as exc:
                with contextlib.suppress(OSError):
                    os.close(master)
                os.close(slave)
                raise AgentAuthError(
                    f"Could not start the sign-in command: {exc}", code="spawn_failed"
                ) from exc
            os.close(slave)
            self._master_fd = master
        else:
            try:
                self._process = subprocess.Popen(
                    self.argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.cwd),
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise AgentAuthError(
                    f"Could not start the sign-in command: {exc}", code="spawn_failed"
                ) from exc
        self.status = "running"
        self._reader = threading.Thread(
            target=self._pump, name=f"agent-auth-{self.kind}", daemon=True
        )
        self._reader.start()
        self._alarm = self.loop.call_later(self.timeout_seconds, self._on_timeout)

    @staticmethod
    def _set_winsize(fd: int, *, cols: int, rows: int) -> None:
        if fcntl is None or termios is None:
            return
        with contextlib.suppress(OSError):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    # -------------------------------------------------------------------- input
    def write(self, data: bytes) -> None:
        if self.status != "running":
            raise AgentAuthError(
                f"This sign-in session is already {self.status}.", code="session_closed"
            )
        try:
            if self._master_fd is not None:
                os.write(self._master_fd, data)
            elif self._process is not None and self._process.stdin is not None:
                self._process.stdin.write(data)
                self._process.stdin.flush()
            else:
                raise AgentAuthError("The session has no input channel.", code="session_closed")
        except OSError as exc:
            raise AgentAuthError(
                f"The sign-in process did not accept input: {exc}", code="input_failed"
            ) from exc

    def resize(self, *, cols: int, rows: int) -> None:
        if self._master_fd is None:
            return
        self._set_winsize(self._master_fd, cols=cols, rows=rows)

    def cancel(self) -> None:
        self._cancelled = True
        self._signal_group(signal.SIGTERM)
        self._append(f"\n[ForgeMesh sent SIGTERM to {self.command}]\n")

    def interrupt(self) -> None:
        """Deliver Ctrl-C as SIGINT as well as the terminal byte.

        A user can press Ctrl-C the instant the terminal opens. Until the child
        has made the pty its controlling terminal the line discipline has no
        foreground group, so the ^C byte is silently dropped and the session
        looks unresponsive. Signalling the process group directly makes the
        interrupt deterministic; the byte is still sent for programs that read
        it as input.
        """

        self._signal_group(signal.SIGINT)

    def _on_timeout(self) -> None:
        if self.status != "running":
            return
        self.error = f"Timed out after {self.timeout_seconds}s and was terminated."
        self._append(f"\n[{self.error}]\n")
        self._signal_group(signal.SIGTERM)
        self.loop.call_later(5.0, self._hard_kill)

    def _hard_kill(self) -> None:
        if self.status != "running":
            return
        self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(process.pid), sig)

    # -------------------------------------------------------------------- output
    def _pump(self) -> None:
        try:
            while True:
                chunk = self._read_chunk()
                if not chunk:
                    break
                self._append(chunk)
        except OSError:
            pass
        except Exception as exc:  # noqa: BLE001 - the reader must always finish
            self.error = f"Output reader failed: {exc}"
        finally:
            code: int | None = None
            if self._process is not None:
                with contextlib.suppress(OSError):
                    code = self._process.wait()
            self._flush_pending()
            self.exit_code = code
            if self._cancelled:
                self.status = "cancelled"
            elif self.error and code not in (0,):
                self.status = "failed"
            elif code == 0:
                self.status = "exited"
            else:
                self.status = "failed"
            self.ended_at = utcnow()
            if self._master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._master_fd)
                self._master_fd = None
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._schedule_exit_hook)

    def _schedule_exit_hook(self) -> None:
        if self._alarm is not None:
            self._alarm.cancel()
            self._alarm = None
        task = self.on_exit(self)
        if asyncio.iscoroutine(task):
            self.loop.create_task(task)

    def _read_chunk(self) -> str:
        if self._master_fd is not None:
            return os.read(self._master_fd, 16_384).decode("utf-8", errors="replace")
        process = self._process
        if process is None or process.stdout is None:
            return ""
        stream = process.stdout
        reader = getattr(stream, "read1", None)
        raw = reader(4096) if callable(reader) else stream.read(1)
        return (raw or b"").decode("utf-8", errors="replace")

    def _append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            buf = self._pending + text
            self._pending = ""
            fragment = _trailing_escape(buf)
            if fragment and not _escape_complete(fragment):
                buf = buf[: -len(fragment)]
                self._pending = fragment
            if len(self._pending) > 128:
                self._write_rendered(_rendered(self._pending))
                self._pending = ""
            self._write_rendered(_rendered(buf))

    def _flush_pending(self) -> None:
        with self._lock:
            if self._pending:
                self._write_rendered(_rendered(self._pending))
                self._pending = ""

    def _write_rendered(self, text: str) -> None:
        if not text:
            return
        self._text += text
        self._total_chars += len(text)
        overflow = len(self._text) - MAX_BUFFER_CHARS
        if overflow > 0:
            self._text = self._text[overflow:]
            self._start_offset += overflow

    def output(self, offset: int) -> dict[str, Any]:
        with self._lock:
            absolute = max(0, int(offset))
            truncated = absolute > 0 and absolute < self._start_offset
            start = max(0, absolute - self._start_offset)
            chunk = self._text[start:]
            return {
                "id": self.id,
                "kind": self.kind,
                "command": self.command,
                "mode": self.mode,
                "status": self.status,
                "exit_code": self.exit_code,
                "error": self.error,
                "text": chunk,
                "next_offset": self._start_offset + len(self._text),
                "first_offset": self._start_offset,
                "total_chars": self._total_chars,
                "truncated": truncated,
                "started_at": iso(self.started_at),
                "ended_at": iso(self.ended_at),
                "timeout_seconds": self.timeout_seconds,
            }

    def summary(self) -> dict[str, Any]:
        """A live badge only; the buffered text stays behind the session endpoint."""
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "command": self.command,
                "mode": self.mode,
                "status": self.status,
                "exit_code": self.exit_code,
                "next_offset": self._start_offset + len(self._text),
                "started_at": iso(self.started_at),
                "ended_at": iso(self.ended_at),
            }

    @property
    def tail(self) -> str:
        with self._lock:
            return self._text[-MAX_TAIL_CHARS:]


def _trailing_escape(buf: str) -> str:
    """A control sequence started at the very end of ``buf``, if any."""
    index = buf.rfind("\x1b")
    if index < 0:
        return ""
    fragment = buf[index:]
    return fragment if len(fragment) <= 128 else ""


def _escape_complete(fragment: str) -> bool:
    """Whether a held-back control sequence is already terminated."""
    if not fragment.startswith("\x1b"):
        return True
    if fragment == "\x1b":
        return False
    if fragment[1] == "[":
        return bool(re.fullmatch(r"\x1b\[[0-9;?]*[ -/]*[@-~]", fragment))
    if fragment[1] == "]":
        return fragment.endswith("\x07") or fragment.endswith("\x1b\\")
    return True


class AgentAuthService:
    """Interactive sign-in sessions plus the accounts the CLIs actually report."""

    def __init__(
        self,
        settings: ControlSettings,
        db: ControlDatabase,
        events: EventStore,
        audit: AuditService,
        registry: Any,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.audit = audit
        self.registry = registry
        self._sessions: dict[str, TerminalSession] = {}
        self._by_kind: dict[str, str] = {}
        self._background_verifies: dict[str, asyncio.Task[Any]] = {}

    # --------------------------------------------------------------------- common
    async def _agent_row(self, kind: str) -> dict[str, Any]:
        row = await self.registry.get_agent(kind)
        if not row.get("installed") or not row.get("path"):
            raise AgentAuthError(
                f"{row.get('display_name') or kind} is not installed, so it cannot be "
                "authenticated. Install it first.",
                code="not_installed",
            )
        return row

    async def _adapter(self, kind: str) -> CodingAgent:
        row = await self._agent_row(kind)
        return self.registry.adapter(kind, row["path"])

    def _account_dir(self, kind: str, name: str) -> Path:
        return (self.settings.agent_config_root / kind / name).resolve()

    # ------------------------------------------------------------------ terminal
    async def start_login(
        self,
        kind: str,
        *,
        provider: str = "",
        account_id: int | None = None,
        cols: int = 100,
        rows: int = 24,
    ) -> dict[str, Any]:
        """Spawn the CLI's own documented login command in a hosted terminal."""
        row = await self._agent_row(kind)
        adapter = self.registry.adapter(kind, row["path"])
        support = adapter.auth_support()
        if not support.get("interactive_login"):
            raise AgentAuthError(
                support.get("limitation")
                or f"{kind} does not offer a supported interactive sign-in.",
                code="unsupported",
            )
        running = self._running_session(kind)
        if running is not None:
            raise AgentAuthError(
                "A sign-in session for this agent is already open. Finish or cancel it "
                "before starting another.",
                code="session_running",
            )

        config_dir = ""
        if account_id is not None:
            account = await self._row_by_id(account_id)
            if account["kind"] != kind:
                raise AgentAuthError(
                    "That account belongs to a different agent.", code="bad_account"
                )
            if support.get("account_isolation") != "config_dir":
                raise AgentAuthError(
                    support.get("limitation")
                    or f"{kind} cannot sign in to a named account directly.",
                    code="unsupported",
                )
            config_dir = account["config_dir"]
            if not config_dir:
                config_dir = ""  # the CLI's own default root

        argv = [row["path"], *adapter.login_argv(provider=provider, config_dir=config_dir)]
        if len(argv) < 2:  # pragma: no cover - guarded by interactive_login
            raise AgentAuthError("No login command is available.", code="unsupported")

        caps = (row.get("metadata") or {}).get("capabilities") or {}
        if kind == "qoder" and caps.get("verified") and caps.get("login_command") is False:
            raise AgentAuthError(
                "The installed Qoder CLI does not list a `login` command, so ForgeMesh "
                "will not guess one.",
                code="unsupported",
            )

        session = TerminalSession(
            kind=kind,
            argv=argv,
            cwd=self.settings.agent_config_root.parent,
            on_exit=self._on_session_exit,
        )
        session.start(
            cols=_clamp(cols, TERMINAL_COLS),
            rows=_clamp(rows, TERMINAL_ROWS),
            timeout_seconds=self.settings.browser_timeout_seconds * 3,
        )
        self._sessions[session.id] = session
        self._by_kind[kind] = session.id
        # Finished buffers stay in memory only for a short look-back; the durable
        # record is the scrubbed tail in the database.
        finished = [item for item in self._sessions.values() if item.status != "running"]
        for stale in sorted(finished, key=lambda item: item.started_at)[: -MAX_MEMORY_SESSIONS]:
            self._sessions.pop(stale.id, None)
        await self._persist_session(session, status="running")
        await self.audit.record(
            "agent.auth.start",
            kind=kind,
            command=session.command,
            config_dir=config_dir,
            provider=provider,
            terminal=session.mode,
        )
        await self.events.publish(
            "agent.auth.started",
            kind=kind,
            session_id=session.id,
            command=session.command,
            mode=session.mode,
        )
        return session.output(0)

    def _running_session(self, kind: str) -> TerminalSession | None:
        session_id = self._by_kind.get(kind)
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is not None and session.status == "running":
            return session
        self._by_kind.pop(kind, None)
        return None

    async def session_view(
        self, kind: str, session_id: str | None, *, offset: int = 0
    ) -> dict[str, Any]:
        session = self._session(kind, session_id)
        if session is not None:
            return session.output(offset)
        stored = await self._stored_session(session_id or self._by_kind.get(kind, ""))
        if stored is None:
            raise AgentAuthError("No such sign-in session.", code="unknown_session")
        return stored

    def _session(self, kind: str, session_id: str | None) -> TerminalSession | None:
        target = session_id or self._by_kind.get(kind)
        session = self._sessions.get(target or "")
        if session is not None and (not kind or session.kind == kind):
            return session
        return None

    async def send_text(self, kind: str, session_id: str | None, text: str) -> dict[str, Any]:
        if not text:
            raise AgentAuthError("Nothing to send.", code="empty_input")
        if len(text) > 4096:
            raise AgentAuthError("Input too long.", code="invalid_input")
        session = self._require_session(kind, session_id)
        session.write(text.encode("utf-8", errors="surrogatepass"))
        await self.audit.record("agent.auth.input", kind=kind, session_id=session.id, bytes=len(text.encode()))
        return session.output(session.output(0)["next_offset"])

    async def send_key(self, kind: str, session_id: str | None, key: str) -> dict[str, Any]:
        sequence = KEY_SEQUENCES.get((key or "").strip().lower())
        if sequence is None:
            raise AgentAuthError(
                f"Unsupported key {key!r}. Allowed: {', '.join(sorted(KEY_SEQUENCES))}.",
                code="invalid_key",
            )
        session = self._require_session(kind, session_id)
        session.write(sequence)
        if sequence == KEY_SEQUENCES["ctrl_c"]:
            session.interrupt()
            await self.audit.record("agent.auth.interrupt", kind=kind, session_id=session.id)
        return session.output(session.output(0)["next_offset"])

    async def resize(
        self, kind: str, session_id: str | None, *, cols: int, rows: int
    ) -> dict[str, Any]:
        session = self._require_session(kind, session_id)
        session.resize(cols=_clamp(cols, TERMINAL_COLS), rows=_clamp(rows, TERMINAL_ROWS))
        return session.output(session.output(0)["next_offset"])

    async def cancel(self, kind: str, session_id: str | None) -> dict[str, Any]:
        session = self._require_session(kind, session_id)
        session.cancel()
        return session.output(0)

    def _require_session(self, kind: str, session_id: str | None) -> TerminalSession:
        session = self._session(kind, session_id)
        if session is None:
            raise AgentAuthError(
                "No open sign-in session for this agent.", code="unknown_session"
            )
        if session.status != "running":
            raise AgentAuthError(
                f"This sign-in session is already {session.status}.", code="session_closed"
            )
        return session

    async def shutdown(self) -> None:
        for task in list(self._background_verifies.values()):
            task.cancel()
        if self._background_verifies:
            await asyncio.gather(
                *self._background_verifies.values(), return_exceptions=True
            )
        for session in list(self._sessions.values()):
            if session.status == "running":
                with contextlib.suppress(Exception):
                    session.cancel()

    def _schedule_functional_verify(self, kind: str) -> None:
        """Turn a successful sign-in into a verified state without another click.

        The status-only re-check after login cannot claim the agent works, which
        leaves it "limited" even though it runs. The harmless real prompt is the
        only honest way to reach "available" (or record a real CLI error), so it
        runs in the background instead of blocking the login result.
        """
        existing = self._background_verifies.get(kind)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._background_verify(kind))
        self._background_verifies[kind] = task

        def _forget(done: asyncio.Task[Any], kind: str = kind) -> None:
            if self._background_verifies.get(kind) is done:
                self._background_verifies.pop(kind, None)

        task.add_done_callback(_forget)

    async def _background_verify(self, kind: str) -> None:
        try:
            row = await self.registry.verify(kind, functional=True)
            logger.info(
                "Post-login functional check for %s: %s",
                kind,
                row.get("availability"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - state stays as the last real check
            logger.warning("Post-login functional check failed for %s: %s", kind, exc)

    # --------------------------------------------------------------- session exit
    async def _on_session_exit(self, session: TerminalSession) -> None:
        detail = redact_output(
            session.error or _first_line(session.tail) or f"exit code {session.exit_code}"
        )
        await self._persist_session(
            session,
            status=session.status,
            exit_code=session.exit_code,
            detail=detail,
            tail=session.tail,
        )
        self._by_kind.pop(session.kind, None)
        verified: dict[str, Any] = {}
        auth_status = "unknown"
        auth_detail = "The sign-in session ended; the CLI status could not be re-read."
        try:
            verified = await self.registry.verify(session.kind, functional=False)
            auth_status = verified.get("auth_status", "unknown")
            auth_detail = redact_output(
                (verified.get("metadata") or {}).get("auth_detail") or auth_detail
            )
        except Exception as exc:  # noqa: BLE001 - a failed re-check is still real state
            logger.warning("Post-login status check failed for %s: %s", session.kind, exc)
            auth_detail = f"Status check failed: {exc}"
        accounts = await self.sync_accounts(session.kind)
        signed_in = auth_status == "authenticated"
        outcome = (
            "signed_in"
            if signed_in and session.status == "exited" and session.exit_code == 0
            else ("still_signed_out" if not signed_in else "check_the_output")
        )
        await self.audit.record(
            "agent.auth.finish",
            kind=session.kind,
            session_id=session.id,
            status=session.status,
            exit_code=session.exit_code,
            auth_status=auth_status,
            outcome=outcome,
        )
        await self.events.publish(
            "agent.auth.finished",
            kind=session.kind,
            session_id=session.id,
            status=session.status,
            exit_code=session.exit_code,
            auth_status=auth_status,
            auth_detail=auth_detail,
            outcome=outcome,
            accounts=len([item for item in accounts if item["status"] == "signed_in"]),
            detail=detail,
        )
        # A verify without an account reads the CLI's default config root, so
        # only the default account can be auto-verified here. Isolated accounts
        # keep their own sign-in result from sync_accounts().
        if signed_in and "--config-dir" not in session.argv:
            self._schedule_functional_verify(session.kind)

    async def _persist_session(
        self,
        session: TerminalSession,
        *,
        status: str,
        exit_code: int | None = None,
        detail: str = "",
        tail: str = "",
    ) -> None:
        async with self.db.session_scope() as session_scope:
            row = await session_scope.get(AgentAuthSessionRow, session.id)
            if row is None:
                row = AgentAuthSessionRow(id=session.id, kind=session.kind)
                session_scope.add(row)
            row.kind = session.kind
            row.command = session.command
            row.status = status
            row.exit_code = exit_code
            row.detail = redact_output(detail)[:1000]
            row.tail = redact_output(tail or session.tail)[-MAX_TAIL_CHARS:]
            row.started_at = session.started_at
            row.ended_at = session.ended_at

    async def _stored_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        async with self.db.session_scope() as scope:
            row = await scope.get(AgentAuthSessionRow, session_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "kind": row.kind,
                "command": row.command,
                "mode": "history",
                "status": row.status,
                "exit_code": row.exit_code,
                "error": row.detail or None,
                "text": row.tail,
                "next_offset": 0,
                "first_offset": 0,
                "truncated": False,
                "started_at": iso(row.started_at),
                "ended_at": iso(row.ended_at),
                "stored": True,
            }

    async def session_history(self, kind: str, *, limit: int = 10) -> list[dict[str, Any]]:
        async with self.db.session_scope() as scope:
            rows = (
                (
                    await scope.execute(
                        select(AgentAuthSessionRow)
                        .where(AgentAuthSessionRow.kind == kind)
                        .order_by(AgentAuthSessionRow.started_at.desc())
                        .limit(max(1, min(limit, 50)))
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "command": row.command,
                "status": row.status,
                "exit_code": row.exit_code,
                "detail": row.detail,
                "tail": row.tail[-HISTORY_TAIL_CHARS:],
                "started_at": iso(row.started_at),
                "ended_at": iso(row.ended_at),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------- accounts
    async def capability(self, kind: str) -> dict[str, Any]:
        """What sign-in and account control is genuinely available right now."""
        row = await self.registry.get_agent(kind)
        if not row.get("installed") or not row.get("path"):
            return {
                "kind": kind,
                "installed": False,
                "interactive_login": False,
                "provider_selection": False,
                "logout": False,
                "multiple_accounts": False,
                "account_isolation": "none",
                "terminal": "unavailable" if not HAS_PTY else "pty",
                "limitation": "The CLI is not installed yet, so authentication cannot run.",
            }
        adapter = self.registry.adapter(kind, row["path"])
        support = dict(adapter.auth_support())
        caps = (row.get("metadata") or {}).get("capabilities") or {}
        if kind == "qoder" and caps.get("verified"):
            support["interactive_login"] = bool(caps.get("login_command", True))
            support["logout"] = bool(caps.get("logout_command", False))
            support["account_isolation"] = (
                "config_dir" if caps.get("config_dir") else "none"
            )
            support["multiple_accounts"] = support["account_isolation"] == "config_dir"
            if not support["interactive_login"]:
                support["limitation"] = (
                    "The installed Qoder CLI does not list a login command, so the "
                    "dashboard will not offer one."
                )
        support.update(
            {
                "kind": kind,
                "installed": True,
                "terminal": "pty" if HAS_PTY else "pipe",
                "terminal_note": (
                    "Interactive prompts are hosted in an embedded terminal."
                    if HAS_PTY
                    else "No pseudo-terminal is available on this platform; interactive "
                    "prompts may not work and the CLI's own error is reported."
                ),
                "auth_status": row.get("auth_status"),
                "availability": row.get("availability"),
            }
        )
        return support

    async def sync_accounts(self, kind: str) -> list[dict[str, Any]]:
        """Rebuild account rows from the CLI's own status output.

        For a CLI with documented config isolation, each managed root is re-read
        with that flag, so a stale row cannot keep showing as signed in.
        """
        try:
            adapter = await self._adapter(kind)
        except AgentAuthError:
            return await self.list_accounts(kind)
        support = adapter.auth_support()
        discovered: list[dict[str, Any]] = []
        try:
            discovered = list(await adapter.list_accounts())
        except Exception as exc:  # noqa: BLE001 - report, never invent
            logger.warning("Account listing failed for %s: %s", kind, exc)
            discovered = []
        async with self.db.session_scope() as scope:
            for item in discovered:
                name = str(item.get("name") or "")[:160]
                if not name:
                    continue
                row = await scope.scalar(
                    select(AgentAccountRow).where(
                        AgentAccountRow.kind == kind, AgentAccountRow.name == name
                    )
                )
                if row is None:
                    row = AgentAccountRow(kind=kind, name=name)
                    scope.add(row)
                row.status = str(item.get("status") or "unknown")
                row.detail = str(item.get("detail") or "")[:500]
                row.source = str(item.get("source") or "cli")
                row.config_dir = str(item.get("config_dir") or "")
                row.switchable = bool(support.get("multiple_accounts"))
                row.last_verified_at = utcnow()
            managed = [
                self._account_dict(row)
                for row in (
                    await scope.execute(
                        select(AgentAccountRow).where(
                            AgentAccountRow.kind == kind,
                            AgentAccountRow.source == "forgemesh_profile",
                        )
                    )
                ).scalars()
            ]

        status_reader = getattr(adapter, "status", None)
        if callable(status_reader) and support.get("account_isolation") == "config_dir":
            for account in managed:
                if not account["config_dir"]:
                    continue
                result = await _maybe_await(status_reader(config_dir=account["config_dir"]))
                identity = str(result.get("identity") or "")
                detail = str(result.get("detail") or "")
                await self._patch_account(
                    account["id"],
                    status=(
                        "signed_in"
                        if result.get("logged_in")
                        else ("unknown" if not result.get("ok") else "signed_out")
                    ),
                    detail=f"{identity} - {detail}".strip(" -") if identity else detail,
                    last_verified_at=utcnow(),
                )

        accounts = await self.list_accounts(kind)
        signed_in = [item for item in accounts if item["status"] == "signed_in"]
        if support.get("multiple_accounts") and signed_in:
            if not any(item["active"] for item in accounts):
                await self._patch_account(signed_in[0]["id"], active=True)
                accounts = await self.list_accounts(kind)
                signed_in = [item for item in accounts if item["status"] == "signed_in"]
        await self.events.publish(
            "agent.accounts.synced",
            kind=kind,
            total=len(accounts),
            signed_in=len(signed_in),
            active=[item["name"] for item in accounts if item["active"]],
            important=False,
        )
        return accounts

    async def list_accounts(self, kind: str) -> list[dict[str, Any]]:
        async with self.db.session_scope() as scope:
            rows = (
                (
                    await scope.execute(
                        select(AgentAccountRow)
                        .where(AgentAccountRow.kind == kind)
                        .order_by(AgentAccountRow.active.desc(), AgentAccountRow.name)
                    )
                )
                .scalars()
                .all()
            )
        return [self._account_dict(row) for row in rows]

    async def _row_by_id(self, account_id: int) -> dict[str, Any]:
        async with self.db.session_scope() as scope:
            row = await scope.get(AgentAccountRow, account_id)
        if row is None:
            raise AgentAuthError("No such agent account.", code="unknown_account")
        return self._account_dict(row)

    async def _patch_account(self, account_id: int, **fields: Any) -> dict[str, Any]:
        async with self.db.session_scope() as scope:
            row = await scope.get(AgentAccountRow, account_id)
            if row is None:
                raise AgentAuthError("No such agent account.", code="unknown_account")
            for key, value in fields.items():
                setattr(row, key, value)
            await scope.flush()
            return self._account_dict(row)

    @staticmethod
    def _account_dict(row: AgentAccountRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "kind": row.kind,
            "name": row.name,
            "status": row.status,
            "detail": row.detail,
            "active": row.active,
            "switchable": row.switchable,
            "config_dir": row.config_dir,
            "source": row.source,
            "last_verified_at": iso(row.last_verified_at),
            "created_at": iso(row.created_at),
        }

    async def add_account(self, kind: str, name: str) -> dict[str, Any]:
        """Create an isolated config root the CLI itself documents."""
        label = (name or "").strip().lower()
        if not ACCOUNT_NAME_RE.fullmatch(label):
            raise AgentAuthError(
                "Account names must be 2-32 characters of a-z, 0-9, '-' or '_'.",
                code="invalid_name",
            )
        adapter = await self._adapter(kind)
        support = adapter.auth_support()
        if support.get("account_isolation") != "config_dir":
            raise AgentAuthError(
                support.get("limitation")
                or f"{kind} does not support more than one account at a time.",
                code="unsupported",
            )
        path = self._account_dir(kind, label)
        if path.exists() and any(path.iterdir()):
            raise AgentAuthError(
                f"A config root named {label!r} already exists with content in it.",
                code="account_exists",
            )
        ensure_private_dir(path)
        async with self.db.session_scope() as scope:
            existing = await scope.scalar(
                select(AgentAccountRow).where(
                    AgentAccountRow.kind == kind, AgentAccountRow.name == label
                )
            )
            if existing is None:
                existing = AgentAccountRow(
                    kind=kind,
                    name=label,
                    status="signed_out",
                    detail=(
                        "Isolated config root created by ForgeMesh. Sign in to activate it."
                    ),
                    config_dir=str(path),
                    source="forgemesh_profile",
                    switchable=True,
                )
                scope.add(existing)
            else:
                existing.config_dir = str(path)
                existing.source = "forgemesh_profile"
            await scope.flush()
            account = self._account_dict(existing)
        await self.audit.record("agent.account.add", kind=kind, name=label, path=str(path))
        await self.events.publish("agent.account.added", kind=kind, account=account)
        return {"account": account, "login_argv": adapter.login_argv(config_dir=str(path))}

    async def set_active(self, kind: str, account_id: int) -> dict[str, Any]:
        adapter = await self._adapter(kind)
        support = adapter.auth_support()
        if not support.get("multiple_accounts"):
            raise AgentAuthError(
                support.get("limitation")
                or f"{kind} has no documented way to choose between accounts.",
                code="unsupported",
            )
        account = await self._row_by_id(account_id)
        if account["kind"] != kind:
            raise AgentAuthError("That account belongs to a different agent.", code="bad_account")
        # Making a signed-out account active would only guarantee that every run
        # fails in the CLI, so the refusal happens here instead.
        if account["status"] == "signed_out":
            raise AgentAuthError(
                f"'{account['name']}' is not signed in yet. Sign it in first; only the "
                "CLI's own status decides when an account can be made active.",
                code="not_signed_in",
            )
        async with self.db.session_scope() as scope:
            rows = (
                (
                    await scope.execute(
                        select(AgentAccountRow).where(AgentAccountRow.kind == kind)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.active = row.id == int(account_id)
            await scope.flush()
        await self.audit.record("agent.account.activate", kind=kind, name=account["name"])
        await self.events.publish(
            "agent.account.active", kind=kind, account_id=account_id, name=account["name"]
        )
        return await self._row_by_id(account_id)

    async def active_account(self, kind: str) -> dict[str, Any] | None:
        """The account a run should use, or None for the CLI's own default."""
        async with self.db.session_scope() as scope:
            row = await scope.scalar(
                select(AgentAccountRow).where(
                    AgentAccountRow.kind == kind, AgentAccountRow.active.is_(True)
                )
            )
        if row is None:
            return None
        if not row.config_dir:
            return None
        return self._account_dict(row)

    async def remove_account(
        self, kind: str, account_id: int, *, confirm: bool, purge: bool = False
    ) -> dict[str, Any]:
        account = await self._row_by_id(account_id)
        if account["kind"] != kind:
            raise AgentAuthError("That account belongs to a different agent.", code="bad_account")
        if not confirm:
            raise AgentAuthError(
                "Removing an agent account needs an explicit confirmation.",
                code="confirmation_required",
            )
        adapter = await self._adapter(kind)
        support = adapter.auth_support()
        removed_credential = False
        note = str(support.get("limitation") or "")
        if support.get("logout") and account["source"] == "cli":
            result = await _maybe_await(adapter.logout(account["name"]))
            removed_credential = bool(result.get("ok"))
            note = str(result.get("detail") or result.get("error") or note)
        elif account["source"] == "forgemesh_profile" and purge:
            root = self.settings.agent_config_root.resolve()
            path = Path(account["config_dir"])
            if root in path.parents and path.is_dir():
                shutil.rmtree(path)
                removed_credential = True
                note = f"Config root {path} was deleted."
            else:
                raise AgentAuthError(
                    "That config root is outside ForgeMesh's own directory.",
                    code="invalid_path",
                )
        async with self.db.session_scope() as scope:
            await scope.execute(
                delete(AgentAccountRow).where(AgentAccountRow.id == account_id)
            )
        promoted: dict[str, Any] | None = None
        if account["active"]:
            remaining = [
                item
                for item in await self.list_accounts(kind)
                if item["status"] == "signed_in"
            ]
            if remaining and support.get("multiple_accounts"):
                promoted = await self.set_active(kind, remaining[0]["id"])
        await self.audit.record(
            "agent.account.remove",
            kind=kind,
            name=account["name"],
            credential_removed=removed_credential,
            purged=purge,
        )
        await self.events.publish(
            "agent.account.removed",
            kind=kind,
            name=account["name"],
            credential_removed=removed_credential,
        )
        return {
            "removed": account["name"],
            "credential_removed": removed_credential,
            "note": note,
            "active_account_promoted": (promoted or {}).get("name"),
        }

    async def logout(self, kind: str, provider: str) -> dict[str, Any]:
        adapter = await self._adapter(kind)
        support = adapter.auth_support()
        if not support.get("logout"):
            raise AgentAuthError(
                support.get("limitation") or f"{kind} offers no logout command.",
                code="unsupported",
            )
        result = await _maybe_await(adapter.logout(provider))
        if not result.get("ok"):
            raise AgentAuthError(
                str(result.get("error") or "The CLI refused to log this provider out."),
                code="logout_failed",
            )
        await self.audit.record("agent.account.logout", kind=kind, provider=provider)
        await self.sync_accounts(kind)
        await self.events.publish("agent.account.logout", kind=kind, provider=provider)
        return {"provider": provider, "detail": result.get("detail", "")}

    async def account_status(self, kind: str) -> dict[str, Any]:
        """Cards for the dashboard: capability plus real, freshly read accounts."""
        capability = await self.capability(kind)
        accounts = await self.list_accounts(kind)
        running = self._running_session(kind)
        return {
            "kind": kind,
            "capability": capability,
            "accounts": accounts,
            "signed_in": len([item for item in accounts if item["status"] == "signed_in"]),
            "active_account": next(
                (item["name"] for item in accounts if item["active"]), None
            ),
            "running_session": running.summary() if running is not None else None,
            "history": await self.session_history(kind),
        }


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    low, high = bounds
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(number, high))


def _first_line(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:300]
    return ""


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value
