"""OpenCode CLI adapter.

Interface verified against OpenCode 1.19.5 on this machine:

* ``opencode --version``
* ``opencode models``
* ``opencode providers list``
* ``opencode providers login [url] -p PROVIDER -m METHOD``
* ``opencode providers logout PROVIDER``
* ``opencode run --format json --dir DIR -m provider/model "prompt"``

The JSON event stream uses ``part.type == "text"`` for assistant text and
carries the session id at the top level. Nothing here invents command flags:
:meth:`get_capabilities` parses the real ``run --help`` output.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forgemesh_control.agents.base import (
    BOX_CHARS,
    AgentRunResult,
    CodingAgent,
    LineHandler,
    agent_env,
    first_version,
    parse_json_lines,
    stream_process,
    strip_ansi,
)

VERIFY_PROMPT = "Reply with exactly: FORGEMESH_VERIFY_OK"
VERIFY_TOKEN = "FORGEMESH_VERIFY_OK"
#: A throttled or refused provider request never reaches the JSON event stream.
#: Measured here with no stored credentials: ``opencode run --format json``
#: printed nothing and did not exit, while the documented
#: ``--print-logs --log-level ERROR`` flags put the CLI's own error line on the
#: pipe. ForgeMesh reads only that pipe; OpenCode's log files are never opened.
PROVIDER_ERROR_RE = re.compile(r'error\.error="([^"]+)"')
#: Once the CLI states one of these, no answer can still arrive, so the check
#: stops instead of waiting out its whole budget.
FATAL_ERROR_HINTS = (
    "usage limit reached",
    "rate limit",
    "quota",
    "not logged in",
    "unauthenticated",
    "authentication required",
    "invalid api key",
)
#: Sign-in hints that come from the CLI's own words. "Free usage limit reached"
#: deliberately matches none of these: a throttled free model is not a request
#: to authenticate.
SIGN_OUT_HINTS = ("not logged in", "unauthenticated", "authentication required")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CREDENTIAL_COUNT_RE = re.compile(r"(\d+)\s+credentials?")
PROVIDER_NAME_RE = re.compile(r"[A-Za-z0-9][\w.-]{0,60}")
# ``providers list`` prints the path of its own credential file. Only the path
# is displayed; the file is never opened by ForgeMesh.
FILE_LINE_RE = re.compile(r"^\s*(?:Credentials|auth)\s+(\S+)", re.IGNORECASE)


def _looks_signed_out(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in SIGN_OUT_HINTS)


class OpenCodeAgent(CodingAgent):
    kind = "opencode"
    display_name = "OpenCode CLI"
    # The CLI reports a throttled free model by log line only, then waits, so a
    # long budget buys nothing: the answer or the error is already on the pipe.
    verify_timeout_seconds = 90

    async def _run(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> tuple[int, list[str], str | None]:
        return await stream_process(
            [str(self.binary), *args],
            cwd=cwd,
            env=agent_env({"OPENCODE_DISABLE_TELEMETRY": "1"}),
            timeout_seconds=timeout_seconds,
        )

    async def get_version(self) -> str:
        code, lines, error = await self._run(["--version"], cwd=Path.home(), timeout_seconds=30)
        if error or code != 0:
            return ""
        return first_version("\n".join(lines))

    async def get_capabilities(self) -> dict[str, Any]:
        code, lines, error = await self._run(
            ["run", "--help"], cwd=Path.home(), timeout_seconds=30
        )
        help_text = "\n".join(lines)
        if error or code != 0:
            return {
                "verified": False,
                "error": error or f"`opencode run --help` exited with {code}",
            }
        return {
            "verified": True,
            "json_events": "--format" in help_text and "json" in help_text,
            "model_selection": "--model" in help_text or "-m" in help_text,
            "directory": "--dir" in help_text,
            "file_attachments": "--file" in help_text or "-f" in help_text,
            "session_resume": "--session" in help_text or "-s" in help_text,
            "non_interactive": "run" in help_text,
            # ``--auto`` exists but auto-approving permissions is never used by
            # ForgeMesh; every write goes through an approval gate.
            "forgemesh_auto_approves": False,
        }

    async def list_models(self) -> list[str]:
        code, lines, error = await self._run(
            ["models"], cwd=Path.home(), timeout_seconds=60
        )
        if error or code != 0:
            return []
        return sorted({line.strip() for line in lines if "/" in line and " " not in line.strip()})

    async def check_auth(self) -> tuple[str, str]:
        accounts = await self._provider_rows()
        if accounts is None:
            return "unknown", "`opencode providers list` could not be run."
        if not accounts:
            return (
                "unauthenticated",
                "OpenCode stores no provider credentials yet. Its free models can "
                "still run; sign in to a provider from Settings -> Coding Agents.",
            )
        names = ", ".join(sorted({str(item["name"]) for item in accounts}))
        return "authenticated", f"OpenCode reports credentials for: {names}."

    # ---------------------------------------------------------------- auth model
    def auth_support(self) -> dict[str, Any]:
        # ``opencode providers login`` is the documented flow. It is interactive
        # (provider list, then either a browser OAuth dance or a pasted key), so
        # ForgeMesh drives it through a real terminal session.
        #
        # OpenCode keeps ONE credential per provider id in its own store, and the
        # CLI documents no way to hold two accounts for the same provider at once
        # or to point a run at a chosen credential. Reporting more would be a
        # fake capability, so multiple_accounts stays False.
        return {
            "interactive_login": True,
            "provider_selection": True,
            "logout": True,
            "multiple_accounts": False,
            "account_isolation": "none",
            # Measured on this machine with `opencode providers list` empty: a
            # free model answered a real prompt in about a second, at cost 0.
            # Signing in is therefore an upgrade choice, not a prerequisite.
            "required_for_basic_use": False,
            "limitation": (
                "OpenCode stores one credential per provider. It has no documented way "
                "to hold two accounts for the same provider simultaneously, so switching "
                "a provider's account means logging out and signing in again."
            ),
        }

    def login_argv(self, *, provider: str = "", config_dir: str = "") -> list[str]:
        argv = ["providers", "login"]
        target = (provider or "").strip()
        if target and PROVIDER_NAME_RE.fullmatch(target):
            argv += ["-p", target]
        return argv

    def logout_argv(self, *, provider: str = "") -> list[str]:
        target = (provider or "").strip()
        if not target or not PROVIDER_NAME_RE.fullmatch(target):
            return []
        return ["providers", "logout", target]

    async def _provider_rows(self) -> list[dict[str, Any]] | None:
        """Parse ``opencode providers list``. Returns None when it could not run.

        Only the provider name and, when the CLI prints one, its account email
        are kept. Key material, tokens and the credential file contents are
        never read or stored.
        """
        code, lines, error = await self._run(
            ["providers", "list"], cwd=Path.home(), timeout_seconds=60
        )
        if error or code != 0:
            return None
        accounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        current: dict[str, Any] | None = None
        for raw in lines:
            text = strip_ansi(raw).translate(dict.fromkeys(map(ord, BOX_CHARS)))
            stripped = text.strip()
            if not stripped or FILE_LINE_RE.match(stripped):
                continue
            if CREDENTIAL_COUNT_RE.search(stripped) and len(stripped.split()) <= 3:
                continue
            name = stripped.split()[0]
            email = EMAIL_RE.search(stripped)
            if not PROVIDER_NAME_RE.fullmatch(name):
                # The CLI may print the account on its own row; attach it to the
                # provider above rather than dropping it.
                if email is not None and current is not None and not current["email"]:
                    current["email"] = email.group(0)
                    current["detail"] = (
                        f"OpenCode stores a credential for {current['name']} "
                        f"({current['email']})"
                    )
                continue
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            current = {
                "name": name,
                "status": "signed_in",
                "email": email.group(0) if email else "",
                "detail": f"OpenCode stores a credential for {name}"
                + (f" ({email.group(0)})" if email else ""),
                "config_dir": "",
                "source": "cli",
            }
            accounts.append(current)
        return accounts

    async def list_accounts(self) -> list[dict[str, Any]]:
        rows = await self._provider_rows()
        return [] if rows is None else rows

    async def logout(self, provider: str) -> dict[str, Any]:
        """Run the documented ``providers logout`` for one provider."""
        argv = self.logout_argv(provider=provider)
        if not argv:
            return {"ok": False, "error": f"Unsupported provider name {provider!r}."}
        code, lines, error = await self._run(argv, cwd=Path.home(), timeout_seconds=60)
        text = "\n".join(strip_ansi(line) for line in lines[-10:])
        if error:
            return {"ok": False, "error": error}
        if code != 0:
            return {"ok": False, "error": f"exited with {code}: {text}"[:400]}
        return {"ok": True, "provider": provider.strip(), "detail": text.strip()}

    def account_argv(self, account: dict[str, Any] | None) -> list[str]:
        """OpenCode has no per-account flag; the credential store is global."""
        return []

    async def login_command(self, provider: str | None) -> list[str]:
        argv = ["providers", "login"]
        if provider:
            candidate = provider.strip()
            if re.fullmatch(r"[\w.-]{1,60}", candidate):
                argv += ["-p", candidate]
        return argv

    @staticmethod
    def _run_argv(
        cwd: Path, model: str | None, prompt: str, files: list[Path] | None = None
    ) -> list[str]:
        """The documented ``run`` call, with the error log on the same pipe.

        ``--print-logs --log-level ERROR`` is what makes a refused provider
        request visible at all: without it the CLI retries silently, emits no
        JSON event and never exits.
        """
        argv = [
            "run",
            "--print-logs",
            "--log-level",
            "ERROR",
            "--format",
            "json",
            "--dir",
            str(cwd),
        ]
        if model:
            argv += ["-m", model]
        for file in files or []:
            argv += ["-f", str(file)]
        argv.append(prompt)
        return argv

    @staticmethod
    def _error_watch() -> tuple[
        Callable[[str], None], Callable[[str], bool], list[str]
    ]:
        """Watch one run for the CLI's own refusal.

        Returns the recorder to hang on the pipe, the predicate that says the
        answer can no longer arrive, and the list of messages the CLI stated.
        """
        errors: list[str] = []

        def record(line: str) -> None:
            match = PROVIDER_ERROR_RE.search(line)
            if match is not None and match.group(1).strip():
                detail = match.group(1).strip()
                if detail not in errors:
                    errors.append(detail)

        def fatal(line: str) -> bool:
            match = PROVIDER_ERROR_RE.search(line)
            if match is None:
                return False
            lowered = match.group(1).lower()
            return any(hint in lowered for hint in FATAL_ERROR_HINTS)

        return record, fatal, errors

    async def _default_model(self) -> str | None:
        """A model the CLI reports it serves without a credential.

        Left to its own default the CLI picks one of the free models that is
        currently throttled, which is why an unattended run can stall: this is
        the same preference the availability check uses.
        """
        models = await self.list_models()
        free = [item for item in models if "free" in item.lower()]
        return (free or models or [None])[0]

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
        started = time.monotonic()
        # ``account`` cannot be honoured: OpenCode picks the credential by
        # provider id, with no documented per-account flag (see account_argv).
        chosen = model or await self._default_model()
        argv = self._run_argv(cwd, chosen, prompt, files)
        record, fatal, provider_errors = self._error_watch()

        def watch(line: str) -> Any:
            record(line)
            return on_line(line) if on_line is not None else None

        code, lines, error = await stream_process(
            [str(self.binary), *argv],
            cwd=cwd,
            env=agent_env({"OPENCODE_DISABLE_TELEMETRY": "1"}),
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            on_line=watch,
            early_exit=fatal,
        )
        events, noise = parse_json_lines(lines)
        text_parts: list[str] = []
        errors: list[str] = []
        refusals: list[str] = []
        session_id: str | None = None
        for event in events:
            session_id = session_id or event.get("sessionID") or event.get("session_id")
            part = event.get("part") or {}
            if event.get("type") == "text" and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str) and value:
                    text_parts.append(value)
            elif event.get("type") == "error":
                detail = event.get("error") or event.get("message") or event
                errors.append(str(detail))
            elif part.get("type") == "tool":
                # A denied tool call is the CLI saying it could not do the work:
                # with no terminal to answer in, OpenCode auto-rejects and the
                # reason rides on the event, never on the text stream.
                state = part.get("state") or {}
                if state.get("status") == "error":
                    detail = state.get("error")
                    if isinstance(detail, str) and detail and detail not in refusals:
                        refusals.append(detail)

        message = error or ("\n".join(errors) if errors else None)
        # The CLI's own sentence beats a generic wrapper: it is what the vendor
        # says about why no answer came, and it is the only explanation
        # available when the refusal never reaches the JSON event stream.
        if message is None and not text_parts:
            if provider_errors:
                message = provider_errors[-1]
            elif refusals:
                message = refusals[-1]
        if message is None and code not in (0, None) and not text_parts:
            tail = "\n".join(noise[-15:]) or "\n".join(lines[-15:])
            message = f"opencode exited with {code}" + (f": {tail}" if tail else "")
        if message is None and not text_parts and not events:
            message = "opencode produced no JSON events and no output."

        return AgentRunResult(
            ok=code == 0 and not errors and bool(text_parts),
            text="\n\n".join(text_parts).strip(),
            exit_code=code,
            duration_ms=int((time.monotonic() - started) * 1000),
            session_id=session_id,
            error=message,
            events=events,
            model=chosen,
        )

    async def functional_check(
        self, *, cwd: Path, timeout_seconds: int | None = None
    ) -> dict[str, Any]:
        """Run a harmless real prompt, preferring a free model.

        Installation success is only ever claimed after this returns
        ``verified`` or an explicit ``auth_required``; the caller stores the
        distinction instead of converting both into "ready".
        """
        chosen = await self._default_model()
        argv = self._run_argv(cwd, chosen, VERIFY_PROMPT)
        record, fatal, provider_errors = self._error_watch()

        started = time.monotonic()
        _, lines, error = await stream_process(
            [str(self.binary), *argv],
            cwd=cwd,
            env=agent_env({"OPENCODE_DISABLE_TELEMETRY": "1"}),
            timeout_seconds=timeout_seconds or self.verify_timeout_seconds,
            on_line=record,
            early_exit=fatal,
        )
        duration_ms = int((time.monotonic() - started) * 1000)

        events, _noise = parse_json_lines(lines)
        text_parts: list[str] = []
        for event in events:
            part = event.get("part") or {}
            if event.get("type") == "text" and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str) and value:
                    text_parts.append(value)
        text = "\n\n".join(text_parts).strip()
        if text:
            return {
                "verified": True,
                "model": chosen,
                "detail": (
                    f"Real prompt returned {VERIFY_TOKEN}"
                    if VERIFY_TOKEN in text
                    else "CLI executed a real prompt and returned text"
                ),
                "duration_ms": duration_ms,
            }
        detail = provider_errors[-1] if provider_errors else (error or "No usable response")
        return {
            "verified": False,
            "auth_required": _looks_signed_out(detail),
            "model": chosen,
            "detail": detail,
            "duration_ms": duration_ms,
        }
