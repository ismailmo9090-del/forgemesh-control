"""Audit trail: database rows plus a JSON-lines file for destructive actions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from forgemesh_browser.storage.database import Database
from forgemesh_browser.storage.repositories import audit as audit_repo

logger = logging.getLogger("forgemesh.audit")


class AuditLogger:
    def __init__(self, db: Database, log_dir: Path) -> None:
        self.db = db
        self.log_dir = log_dir
        self.path = log_dir / "audit.jsonl"

    async def record(
        self, action: str, *, actor: str = "cli", details: dict | None = None
    ) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "actor": actor,
            "action": action,
            "details": details or {},
        }
        try:
            async with self.db.session_scope() as session:
                await audit_repo.add(
                    session, action, actor=actor, details=details or {}
                )
        except Exception:  # pragma: no cover - audit must not break the action
            logger.exception("Failed to persist audit event %s", action)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover
            logger.exception("Failed to append audit file")
