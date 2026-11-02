"""The ordered control brain: Qoder first, OpenCode as the fallback.

The brain writes text - a meeting brief and a final polish - and never invents
facts. Browser automation itself is still executed by the ForgeMesh Browser
runtime; the brain only decides what the council is asked and how the stored
result is explained. Every run records which CLI actually answered, so the
dashboard never claims Qoder did work that OpenCode performed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgemesh_control.config import ControlSettings

logger = logging.getLogger(__name__)

#: The operator's fixed order. Qoder handles the task; only a real failure
#: (limit, auth, timeout, crash, empty answer) moves the task to OpenCode.
BRAIN_ORDER = ("qoder", "opencode")


@dataclass(slots=True)
class BrainAttempt:
    kind: str
    ok: bool
    duration_ms: int
    detail: str | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "model": self.model,
        }


@dataclass(slots=True)
class BrainResult:
    ok: bool
    text: str = ""
    kind: str | None = None
    error: str | None = None
    attempts: list[BrainAttempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "kind": self.kind,
            "error": self.error,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


class BrainService:
    """Runs one bounded text prompt through the ordered coding agents."""

    def __init__(
        self,
        settings: ControlSettings,
        registry: Any,
        *,
        account_lookup: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.account_lookup = account_lookup
        self._workspace = Path(settings.data_dir) / "brain"

    @property
    def enabled(self) -> bool:
        return bool(self.settings.brain_enabled)

    def _prepare_workspace(self) -> Path:
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._workspace.chmod(0o700)
        return self._workspace

    async def run(
        self,
        prompt: str,
        *,
        purpose: str,
        timeout_seconds: int | None = None,
    ) -> BrainResult:
        attempts: list[BrainAttempt] = []
        if not self.enabled:
            return BrainResult(
                ok=False,
                error="The control brain is disabled in settings.",
                attempts=attempts,
            )
        if not prompt.strip():
            return BrainResult(ok=False, error="Empty brain prompt.", attempts=attempts)

        workspace = self._prepare_workspace()
        timeout = timeout_seconds or self.settings.brain_timeout_seconds

        for kind in BRAIN_ORDER:
            started = time.monotonic()
            try:
                info = await self.registry.get_agent(kind)
            except Exception as exc:  # noqa: BLE001 - try the next brain
                attempts.append(
                    BrainAttempt(
                        kind=kind,
                        ok=False,
                        duration_ms=0,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if not info.get("enabled", False):
                attempts.append(
                    BrainAttempt(kind=kind, ok=False, duration_ms=0, detail="disabled")
                )
                continue
            # "limited" means installed and executable but not functionally
            # verified (for example right after a sign-in where the prompt was
            # skipped). Running it is the only honest way to know, and any
            # failure is reported with the CLI's own text.
            if info.get("availability") not in {"available", "limited"}:
                attempts.append(
                    BrainAttempt(
                        kind=kind,
                        ok=False,
                        duration_ms=0,
                        detail=(
                            f"unavailable (availability={info.get('availability')}, "
                            f"auth={info.get('auth_status')}): "
                            f"{info.get('last_error') or 'no recorded reason'}"
                        ),
                    )
                )
                continue
            path = info.get("path")
            if not path:
                attempts.append(
                    BrainAttempt(kind=kind, ok=False, duration_ms=0, detail="no executable path")
                )
                continue

            account: dict[str, Any] | None = None
            if self.account_lookup is not None:
                try:
                    account = await self.account_lookup(kind)
                except Exception as exc:  # noqa: BLE001 - the CLI default still works
                    logger.warning("Brain account lookup failed for %s: %s", kind, exc)

            try:
                adapter = self.registry.adapter(kind, path)
                result = await asyncio.wait_for(
                    adapter.submit_task(
                        prompt,
                        cwd=workspace,
                        timeout_seconds=timeout,
                        account=account,
                    ),
                    timeout=timeout + 15,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                attempts.append(
                    BrainAttempt(
                        kind=kind,
                        ok=False,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        detail=f"timed out after {timeout}s",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - a crash is a fallback trigger
                attempts.append(
                    BrainAttempt(
                        kind=kind,
                        ok=False,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            duration = int((time.monotonic() - started) * 1000)
            text = (result.text or "").strip()
            if result.ok and text:
                attempts.append(
                    BrainAttempt(
                        kind=kind,
                        ok=True,
                        duration_ms=duration,
                        detail=None,
                        model=result.model,
                    )
                )
                return BrainResult(
                    ok=True, text=text, kind=kind, attempts=attempts
                )
            attempts.append(
                BrainAttempt(
                    kind=kind,
                    ok=False,
                    duration_ms=duration,
                    detail=result.error or "the CLI returned no text",
                    model=result.model,
                )
            )
            logger.warning(
                "Brain %s failed for %s; falling back: %s",
                kind,
                purpose,
                attempts[-1].detail,
            )

        return BrainResult(
            ok=False,
            error="Neither Qoder nor OpenCode produced an answer.",
            attempts=attempts,
        )
