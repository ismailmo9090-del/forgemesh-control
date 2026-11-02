"""Account status persistence: a status change must survive the transaction."""

from __future__ import annotations

from forgemesh_browser.accounts.session_health import (
    STATUS_AVAILABLE,
    STATUS_NEEDS_REAUTH,
)
from forgemesh_browser.core.runtime import Runtime


async def test_status_update_is_persisted(settings) -> None:
    runtime = Runtime(settings)
    await runtime.start()
    try:
        await runtime.profiles.create("acct")
        info = await runtime.accounts._set_status(
            "acct", "deepseek", STATUS_AVAILABLE, ok=True
        )
        assert info.status == STATUS_AVAILABLE
        assert info.last_ok_at is not None

        stored = await runtime.accounts.get("acct", "deepseek")
        assert stored.status == STATUS_AVAILABLE

        summary = await runtime.accounts.summary()
        assert summary.total == 1
        assert summary.available == 1
        assert summary.unknown == 0
    finally:
        await runtime.stop()


async def test_error_status_is_persisted(settings) -> None:
    runtime = Runtime(settings)
    await runtime.start()
    try:
        await runtime.profiles.create("acct")
        info = await runtime.accounts._set_status(
            "acct", "deepseek", STATUS_NEEDS_REAUTH, error="Session expired"
        )
        assert info.status == STATUS_NEEDS_REAUTH
        assert info.last_error == "Session expired"

        stored = await runtime.accounts.get("acct", "deepseek")
        assert stored.status == STATUS_NEEDS_REAUTH
        assert stored.last_error == "Session expired"
    finally:
        await runtime.stop()
