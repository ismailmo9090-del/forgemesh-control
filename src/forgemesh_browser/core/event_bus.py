"""In-process event bus used by the API, CLI logs and WebSocket clients."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "data": self.data,
        }


class EventBus:
    def __init__(self, history_size: int = 500) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: deque[Event] = deque(maxlen=history_size)

    def publish(self, event_type: str, **data: Any) -> Event:
        event = Event(type=event_type, data=data)
        self._history.append(event)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(event)
        return event

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return [event.to_dict() for event in list(self._history)[-limit:]]

    @contextlib.asynccontextmanager
    async def subscribe(self, maxsize: int = 256) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    async def stream(self) -> AsyncIterator[Event]:
        async with self.subscribe() as queue:
            while True:
                yield await queue.get()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
