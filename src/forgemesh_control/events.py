"""Durable event system: persist first, then broadcast.

The dashboard reconnects with ``since`` and receives missed events from SQLite.
In-memory history is only an optimisation for the first snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from forgemesh_control.models import EventRow, iso
from forgemesh_control.storage import ControlDatabase

MAX_IN_MEMORY = 300


@dataclass(slots=True)
class Event:
    id: int
    type: str
    data: dict[str, Any]
    timestamp: datetime
    important: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "data": self.data,
            "timestamp": iso(self.timestamp),
            "important": self.important,
        }


class EventStore:
    def __init__(self, db: ControlDatabase) -> None:
        self.db = db
        self._recent: deque[Event] = deque(maxlen=MAX_IN_MEMORY)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._lock = asyncio.Lock()

    async def publish(
        self, event_type: str, *, important: bool = True, **data: Any
    ) -> Event:
        now = datetime.now(UTC)
        async with self._lock:
            async with self.db.session_scope() as session:
                row = EventRow(
                    type=event_type,
                    payload=data,
                    important=important,
                    created_at=now,
                )
                session.add(row)
                await session.flush()
                event_id = row.id
        event = Event(event_id, event_type, data, now, important)
        self._recent.append(event)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(event)
        return event

    async def history(self, limit: int = 100, *, since: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        async with self.db.session_scope() as session:
            statement = (
                select(EventRow)
                .where(EventRow.id > since)
                .order_by(EventRow.id.desc())
                .limit(limit)
            )
            rows = (await session.execute(statement)).scalars().all()
        events = [
            Event(
                row.id,
                row.type,
                row.payload or {},
                row.created_at,
                row.important,
            )
            for row in rows
        ]
        events.sort(key=lambda item: item.id)
        return [event.to_dict() for event in events]

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._recent:
            items = list(self._recent)[-limit:]
            return [event.to_dict() for event in items]
        return await self.history(limit)

    @contextlib.asynccontextmanager
    async def subscribe(self, maxsize: int = 500) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def prune(self, keep: int = 20_000) -> int:
        """Bound the table without losing the newest records."""
        async with self.db.session_scope() as session:
            newest = (
                await session.execute(select(EventRow.id).order_by(EventRow.id.desc()).limit(keep))
            ).scalars().all()
            if not newest:
                return 0
            cutoff = min(newest)
            result = await session.execute(delete(EventRow).where(EventRow.id < cutoff))
            return int(result.rowcount or 0)
