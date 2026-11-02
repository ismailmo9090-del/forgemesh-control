"""Event bus fan-out and task journal persistence."""

from __future__ import annotations

import asyncio

import pytest

from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import AutomationError
from forgemesh_browser.core.task_manager import TaskManager
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database


async def test_event_bus_history_and_subscription() -> None:
    bus = EventBus(history_size=2)
    bus.publish("first", value=1)
    bus.publish("second", value=2)

    async with bus.subscribe() as queue:
        bus.publish("third", value=3)
        event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.type == "third"
    assert event.data == {"value": 3}
    assert bus.subscriber_count == 0

    history = bus.history()
    assert [item["type"] for item in history] == ["second", "third"]


async def test_event_to_dict() -> None:
    event = EventBus().publish("x", answer=42)
    data = event.to_dict()
    assert data["type"] == "x"
    assert data["data"] == {"answer": 42}
    assert data["id"] and data["timestamp"]


async def test_task_manager_success(settings: Settings) -> None:
    db = Database(settings.database_path)
    await db.init()
    events = EventBus()
    tasks = TaskManager(db, events)
    try:
        async def operation() -> dict:
            return {"ok": True, "count": 2}

        record = await tasks.execute("demo", operation, {"profile": "x"})
        assert record["status"] == "completed"
        assert record["result"] == {"ok": True, "count": 2}
        stored = await tasks.get(record["task_id"])
        assert stored["status"] == "completed"
        assert stored["params"] == {"profile": "x"}
        assert await tasks.list_recent() != []
        assert any(event["type"] == "task.completed" for event in events.history())
    finally:
        await db.close()


async def test_task_manager_failure_is_journaled(settings: Settings) -> None:
    db = Database(settings.database_path)
    await db.init()
    tasks = TaskManager(db, EventBus())
    try:
        async def operation() -> dict:
            raise AutomationError("element missing")

        with pytest.raises(AutomationError) as info:
            await tasks.execute("demo", operation, {"x": object()})

        assert "task_id" in info.value.details
        stored = await tasks.get(info.value.details["task_id"])
        assert stored["status"] == "failed"
        assert stored["error"] == "element missing"
        assert isinstance(stored["params"]["x"], str)
    finally:
        await db.close()
