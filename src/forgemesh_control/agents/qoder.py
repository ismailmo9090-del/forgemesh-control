"""Qoder CLI adapter.

Interface verified against Qoder CLI 1.1.81 on this machine:

* ``qodercli --version``
* ``qodercli status`` and ``qodercli status -o json`` (``logged_in`` plus version)
* ``qodercli login`` (opens the official sign-in flow)
* ``qodercli --config-dir DIR <command>`` — a documented custom user-level
  config root, which is what makes one isolated account per ForgeMesh
  profile real rather than decorative.
* ``qodercli --list-models``
* ``qodercli -p -o json --permission-mode accept_edits --cwd DIR "prompt"``

The official installer places a ``qodercli`` entry point. The desktop Qoder
application also ships a ``qoder`` binary under ``/opt/Qoder``; that is a GUI,
not this CLI, and is deliberately never detected as an agent.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from forgemesh_control.agents.base import (
    AgentRunResult,
    CodingAgent,
    LineHandler,
    agent_env,
    first_version,
    parse_json_lines,
    stream_process,
)

VERIFY_PROMPT = "Reply with exactly: FORGEMESH_VERIFY_OK"
VERIFY_TOKEN = "FORGEMESH_VERIFY_OK"
LOGIN_HINTS = ("not logged in", "please run /login", "run `qodercli login`")
CONFIG_DIR_RE = re.compile(r"/[A-Za-z0-9._/-]{1,250}")
# The identity keys Qoder's own JSON status may use. Credential fields are
# deliberately never read, even though they live in the same object.
IDENTITY_KEYS = ("account", "email", "user", "username", "handle")
CREDENTIAL_KEYS = ("token", "key", "secret", "cookie", "password", "authorization")


def _looks_logged_out(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in LOGIN_HINTS)


def _identity(payload: dict[str, Any]) -> str:
    """The account label Qoder reports, or an empty string.

    Only non-secret identity fields are considered, and anything that looks
    like a token is dropped.
    """
    for key, value in payload.items():
        lowered = str(key).lower()
        if any(bad in lowered for bad in CREDENTIAL_KEYS):
            continue
        if lowered in IDENTITY_KEYS and isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return ""


def _config_dir_or_empty(value: Any) -> str:
    """Only an absolute path without traversal may reach the CLI."""
    text = str(value or "").strip()
    if not text or not text.startswith("/") or ".." in Path(text).parts:
        return ""
    return text if CONFIG_DIR_RE.fullmatch(text) else ""


def _has_command(help_text: str, name: str) -> bool:
    """True only when the CLI itself lists this command under ``Commands:``."""
    _, separator, commands = help_text.partition("Commands:")
    if not separator:
        return False
    return bool(re.search(rf"(?m)^\s+{re.escape(name)}\b", commands))


class QoderAgent(CodingAgent):
    kind = "qoder"
    display_name = "Qoder CLI"

    async def _run(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        config_dir: str | None = None,
    ) -> tuple[int, list[str], str | None]:
        argv = list(args)
        target = _config_dir_or_empty(config_dir)
        if target:
            argv = ["--config-dir", target, *argv]
        return await stream_process(
            [str(self.binary), *argv],
            cwd=cwd,
            env=agent_env({"QODER_CLI_DISABLE_TELEMETRY": "1"}),
            timeout_seconds=timeout_seconds,
        )

    async def status(
        self, *, config_dir: str | None = None, timeout_seconds: int = 60
    ) -> dict[str, Any]:
        """Ask the CLI itself whether this config root is signed in.

        Returns ``{"ok": bool, "logged_in": bool, "identity": str, "detail": str}``.
        ``ok`` is False when the command could not be interpreted at all, which
        is reported as unknown rather than as signed out.
        """
        code, lines, error = await self._run(
            ["status", "-o", "json"],
            cwd=Path.home(),
            timeout_seconds=timeout_seconds,
            config_dir=config_dir,
        )
        text = "\n".join(lines)
        if error:
            return {"ok": False, "logged_in": False, "identity": "", "detail": error}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "logged_in" in payload:
            if payload.get("logged_in") is True:
                return {
                    "ok": True,
                    "logged_in": True,
                    "identity": _identity(payload),
                    "detail": "Qoder CLI reports a signed-in account for this config root.",
                }
            return {
                "ok": True,
                "logged_in": False,
                "identity": "",
                "detail": "Qoder CLI is signed out for this config root.",
            }
        # Older/newer builds may only print text; fall back to the same
        # heuristics the text status check has always used.
        if code != 0:
            return {
                "ok": False,
                "logged_in": False,
                "identity": "",
                "detail": f"`qodercli status` exited with {code}",
            }
        if _looks_logged_out(text):
            return {
                "ok": True,
                "logged_in": False,
                "identity": "",
                "detail": "Qoder CLI is signed out for this config root.",
            }
        account = re.search(r"(?im)^\s*account:\s*(.+)$", text)
        if account is not None:
            return {
                "ok": True,
                "logged_in": True,
                "identity": account.group(1).strip()[:120],
                "detail": "Qoder CLI reports a signed-in account for this config root.",
            }
        return {
            "ok": False,
            "logged_in": False,
            "identity": "",
            "detail": "Could not interpret `qodercli status` output.",
        }

    async def get_version(self) -> str:
        code, lines, error = await self._run(["--version"], cwd=Path.home(), timeout_seconds=30)
        if error or code != 0:
            return ""
        return first_version("\n".join(lines))

    async def get_capabilities(self) -> dict[str, Any]:
        code, lines, error = await self._run(["--help"], cwd=Path.home(), timeout_seconds=30)
        help_text = "\n".join(lines)
        if error or code != 0:
            return {
                "verified": False,
                "error": error or f"`qodercli --help` exited with {code}",
            }
        return {
            "verified": True,
            "non_interactive": "--print" in help_text,
            "json_output": "--output-format" in help_text,
            "model_selection": "--model" in help_text,
            "directory": "--cwd" in help_text,
            "file_attachments": "--attachment" in help_text,
            "permission_mode": "--permission-mode" in help_text,
            "session_id": "--session-id" in help_text,
            # This version has no ``--max-turns`` flag; ForgeMesh only passes
            # flags that its own help output advertises.
            "max_turns": "--max-turns" in help_text,
            # Auth surface, read from the documented command list rather than
            # assumed. ``logout`` is absent in 1.1.81 and reported as such.
            "login_command": _has_command(help_text, "login"),
            "status_command": _has_command(help_text, "status"),
            "logout_command": _has_command(help_text, "logout"),
            "config_dir": "--config-dir" in help_text,
            # Approval gates stay in ForgeMesh; the CLI never receives
            # ``--dangerously-skip-permissions`` or ``bypass_permissions``.
            "forgemesh_auto_approves": False,
        }

    async def list_models(self) -> list[str]:
        return await self.models_for(None)

    async def models_for_account(self, account: dict[str, Any] | None) -> list[str]:
        return await self.models_for(_config_dir_or_empty((account or {}).get("config_dir")))

    async def check_auth(self) -> tuple[str, str]:
        status = await self.status()
        if not status["ok"]:
            return "unknown", status["detail"]
        if status["logged_in"]:
            return "authenticated", status["detail"]
        return (
            "unauthenticated",
            "Qoder CLI is not signed in. Run `qodercli login` (official flow) "
            "before Qoder models can be used.",
        )

    # ---------------------------------------------------------------- auth model
    def auth_support(self) -> dict[str, Any]:
        # ``qodercli login`` is the documented sign-in flow and
        # ``--config-dir DIR`` is the documented custom config root, so one
        # isolated signed-in account per directory is real. There is, however,
        # no logout command in this CLI, and that limitation is reported
        # instead of being worked around by deleting credentials silently.
        return {
            "interactive_login": True,
            "provider_selection": False,
            "logout": False,
            "multiple_accounts": True,
            "account_isolation": "config_dir",
            "required_for_basic_use": True,
            "limitation": (
                "Qoder CLI has no logout command. An isolated account can be used, "
                "made active or removed from ForgeMesh, but its stored session stays "
                "in its own config directory until Qoder itself expires it."
            ),
        }

    async def list_accounts(self) -> list[dict[str, Any]]:
        """The account slot at Qoder's default config root."""
        status = await self.status()
        return [
            {
                "name": "default",
                "status": (
                    "signed_in"
                    if status["logged_in"]
                    else ("signed_out" if status["ok"] else "unknown")
                ),
                "email": status["identity"],
                "detail": status["detail"],
                "config_dir": "",
                "source": "default_config",
            }
        ]

    def login_argv(self, *, provider: str = "", config_dir: str = "") -> list[str]:
        argv: list[str] = []
        target = _config_dir_or_empty(config_dir)
        if target:
            argv += ["--config-dir", target]
        return [*argv, "login"]

    def account_argv(self, account: dict[str, Any] | None) -> list[str]:
        """The documented flag that makes a run use one account's config root."""
        if not account:
            return []
        target = _config_dir_or_empty(account.get("config_dir"))
        return ["--config-dir", target] if target else []

    async def models_for(self, config_dir: str | None) -> list[str]:
        code, lines, error = await self._run(
            ["--list-models"],
            cwd=Path.home(),
            timeout_seconds=90,
            config_dir=config_dir,
        )
        if error or code != 0:
            return []
        text = "\n".join(lines)
        if _looks_logged_out(text):
            return []
        models: list[str] = []
        for line in lines:
            stripped = line.strip().lstrip("-*• ").strip()
            if not stripped or " " in stripped:
                continue
            models.append(stripped)
        return sorted(set(models))

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
        argv = [
            *self.account_argv(account),
            "-p",
            "-o",
            "json",
            "--permission-mode",
            "accept_edits",
            "--cwd",
            str(cwd),
        ]
        if model:
            argv += ["-m", model]
        for file in files or []:
            argv += ["--attachment", str(file)]
        argv.append(prompt)

        code, lines, error = await stream_process(
            [str(self.binary), *argv],
            cwd=cwd,
            env=agent_env({"QODER_CLI_DISABLE_TELEMETRY": "1"}),
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            on_line=on_line,
        )
        events, noise = parse_json_lines(lines)
        text_parts: list[str] = []
        errors: list[str] = []
        session_id: str | None = None
        exit_error = False
        for event in events:
            session_id = session_id or event.get("session_id") or event.get("sessionId")
            if event.get("is_error"):
                exit_error = True
            kind = event.get("type")
            if kind == "result":
                value = event.get("result")
                if isinstance(value, str) and value:
                    text_parts.append(value)
                if event.get("is_error"):
                    detail = event.get("result") or event.get("subtype") or "error"
                    errors.append(str(detail))
            elif kind == "assistant":
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        value = block.get("text")
                        if isinstance(value, str) and value:
                            text_parts.append(value)
            elif kind == "error":
                errors.append(str(event.get("error") or event.get("message") or event))

        message = error or ("\n".join(errors) if errors else None)
        if message is None and code not in (0, None) and not text_parts:
            tail = "\n".join(noise[-15:]) or "\n".join(lines[-15:])
            message = f"qodercli exited with {code}" + (f": {tail}" if tail else "")

        # De-duplicate: a result event repeats the text that assistant events
        # already streamed in some Qoder versions.
        deduped: list[str] = []
        for part in text_parts:
            if not deduped or deduped[-1] != part:
                deduped.append(part)

        return AgentRunResult(
            ok=code == 0 and not exit_error and not errors and bool(deduped),
            text="\n\n".join(deduped).strip(),
            exit_code=code,
            duration_ms=int((time.monotonic() - started) * 1000),
            session_id=session_id,
            error=message,
            events=events,
        )

    async def functional_check(
        self, *, cwd: Path, timeout_seconds: int = 180
    ) -> dict[str, Any]:
        models = await self.list_models()
        chosen = models[0] if models else None
        result = await self.submit_task(
            VERIFY_PROMPT,
            cwd=cwd,
            model=chosen,
            timeout_seconds=timeout_seconds,
        )
        if result.ok and (VERIFY_TOKEN in result.text or result.text):
            return {
                "verified": True,
                "model": chosen,
                "detail": (
                    f"Real prompt returned {VERIFY_TOKEN}"
                    if VERIFY_TOKEN in result.text
                    else "CLI executed a real prompt and returned text"
                ),
                "duration_ms": result.duration_ms,
            }
        auth_required = _looks_logged_out(result.error or "") or _looks_logged_out(
            result.text
        )
        return {
            "verified": False,
            "auth_required": auth_required,
            "model": chosen,
            "detail": result.error or "No usable response",
            "duration_ms": result.duration_ms,
        }
