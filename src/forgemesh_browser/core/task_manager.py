"""Task journal used by API/CLI operations for observability."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import ForgeMeshError
from forgemesh_browser.storage.database import Database
from forgemesh_browser.storage.models import TaskRow, as_utc
from forgemesh_browser.storage.repositories import tasks as task_repo


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def task_to_dict(row: TaskRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "profile_id": row.profile_id,
        "provider_id": row.provider_id,
        "params": json.loads(row.params_json or "{}"),
        "result": json.loads(row.result_json) if row.result_json else None,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "created_at": as_utc(row.created_at).isoformat() if row.created_at else None,
        "started_at": as_utc(row.started_at).isoformat() if row.started_at else None,
        "finished_at": as_utc(row.finished_at).isoformat() if row.finished_at else None,
    }


class TaskManager:
    def __init__(self, db: Database, events: EventBus) -> None:
        self.db = db
        self.events = events

    async def execute(
        self,
        kind: str,
        operation: Callable[[], Awaitable[Any]],
        params: dict[str, Any] | None = None,
        *,
        profile_id: str | None = None,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        safe_params = _json_safe(params or {})
        started_wall = datetime.now(UTC)
        started = time.monotonic()
        async with self.db.session_scope() as session:
            await task_repo.create(
                session,
                task_id,
                kind,
                safe_params,
                profile_id=profile_id,
                provider_id=provider_id,
            )
            row = await task_repo.get(session, task_id)
            if row:
                row.status = "running"
                row.started_at = started_wall
        self.events.publish(
            "task.started",
            task_id=task_id,
            kind=kind,
            profile=profile_id,
            provider=provider_id,
        )
        try:
            result = await operation()
        except Exception as exc:
            duration = round((time.monotonic() - started) * 1000, 2)
            async with self.db.session_scope() as session:
                row = await task_repo.get(session, task_id)
                if row:
                    row.status = "failed"
                    row.error = str(exc)
                    row.finished_at = datetime.now(UTC)
                    row.duration_ms = duration
            self.events.publish(
                "task.failed", task_id=task_id, kind=kind, error=str(exc), duration_ms=duration
            )
            if isinstance(exc, ForgeMeshError):
                exc.details.setdefault("task_id", task_id)
            raise
        duration = round((time.monotonic() - started) * 1000, 2)
        safe_result = _json_safe(result)
        async with self.db.session_scope() as session:
            row = await task_repo.get(session, task_id)
            if row:
                row.status = "completed"
                row.result_json = json.dumps(safe_result, default=str)
                row.finished_at = datetime.now(UTC)
                row.duration_ms = duration
        self.events.publish(
            "task.completed", task_id=task_id, kind=kind, duration_ms=duration
        )
        return {
            "task_id": task_id,
            "status": "completed",
            "kind": kind,
            "duration_ms": duration,
            "result": safe_result,
        }

    async def get(self, task_id: str) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await task_repo.get(session, task_id)
            if row is None:
                raise KeyError(task_id)
            return task_to_dict(row)

    async def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            rows = await task_repo.list_recent(session, limit=limit)
            return [task_to_dict(row) for row in rows]
