"""Audit trail: every privileged action is recorded, without secrets."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgemesh_control.models import AuditRow
from forgemesh_control.storage import ControlDatabase

logger = logging.getLogger("forgemesh.control.audit")

_SENSITIVE_KEYS = {
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "private_key",
}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SENSITIVE_KEYS):
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = _clean(item)
        return cleaned
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


class AuditService:
    def __init__(self, db: ControlDatabase, logs_dir: Path) -> None:
        self.db = db
        self.logs_dir = logs_dir
        self.path = logs_dir / "audit.jsonl"

    async def record(
        self, action: str, *, actor: str = "dashboard", **details: Any
    ) -> None:
        clean = _clean(details)
        async with self.db.session_scope() as session:
            session.add(AuditRow(actor=actor, action=action, details=clean))
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "actor": actor,
                    "action": action,
                    "details": clean,
                },
                ensure_ascii=False,
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:  # pragma: no cover - disk failure should not break requests
            logger.warning("Could not append to the audit log at %s", self.path)
