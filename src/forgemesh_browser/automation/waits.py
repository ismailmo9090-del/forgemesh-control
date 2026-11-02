"""Explicit waiting helpers that never mask a failure as success."""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from forgemesh_browser.core.exceptions import AutomationError


async def wait_seconds(seconds: float, *, reason: str | None = None) -> dict[str, Any]:
    if seconds < 0 or seconds > 600:
        raise AutomationError("Wait duration must be between 0 and 600 seconds")
    await asyncio.sleep(seconds)
    return {"waited_seconds": seconds, "reason": reason}


async def wait_for_selector(
    page: Page, selector: str, *, state: str = "visible", timeout_ms: int = 15_000
) -> dict[str, Any]:
    if state not in {"attached", "detached", "hidden", "visible"}:
        raise AutomationError("state must be attached, detached, hidden or visible")
    try:
        await page.locator(selector).first.wait_for(state=state, timeout=timeout_ms)  # type: ignore[arg-type]
    except PlaywrightTimeoutError as exc:
        raise AutomationError(
            f"Selector {selector!r} did not reach state '{state}' within {timeout_ms}ms"
        ) from exc
    return {"selector": selector, "state": state, "timeout_ms": timeout_ms}


async def wait_for_text(
    page: Page, text: str, *, timeout_ms: int = 15_000, exact: bool = False
) -> dict[str, Any]:
    try:
        await page.get_by_text(text, exact=exact).first.wait_for(
            state="visible", timeout=timeout_ms
        )
    except PlaywrightTimeoutError as exc:
        raise AutomationError(
            f"Text {text!r} was not visible within {timeout_ms}ms"
        ) from exc
    return {"text": text, "exact": exact, "timeout_ms": timeout_ms}
