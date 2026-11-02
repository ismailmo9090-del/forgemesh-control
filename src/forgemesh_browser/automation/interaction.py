"""Click, type, fill, select, scroll and keyboard interaction."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from forgemesh_browser.automation.selectors import resolve
from forgemesh_browser.core.browser_manager import BrowserManager
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import AutomationError
from forgemesh_browser.settings import Settings


class InteractionEngine:
    def __init__(
        self, settings: Settings, browser: BrowserManager, events: EventBus
    ) -> None:
        self.settings = settings
        self.browser = browser
        self.events = events

    async def _page(self, profile: str, tab_id: str | None) -> Page:
        return await self.browser.page(profile, tab_id)

    async def _resolve(self, page: Page, target: str):
        return await resolve(
            page, target, timeout_ms=self.settings.action_timeout_ms
        )

    async def _settle(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=2_000)
        except PlaywrightTimeoutError:
            pass

    def _publish(self, action_name: str, profile: str, **data: Any) -> None:
        data.pop("action", None)
        self.events.publish(
            "automation.action", action=action_name, profile=profile, **data
        )

    async def click(
        self,
        profile: str,
        target: str,
        *,
        tab_id: str | None = None,
        click_count: int = 1,
        button: str = "left",
    ) -> dict[str, Any]:
        page = await self._page(profile, tab_id)
        resolved = await self._resolve(page, target)
        try:
            await resolved.locator.click(
                timeout=self.settings.action_timeout_ms,
                click_count=max(1, min(click_count, 3)),
                button=button,  # type: ignore[arg-type]
            )
        except PlaywrightError as exc:
            raise AutomationError(f"Click failed on {target!r}: {exc}") from exc
        await self._settle(page)
        result = {
            "action": "click",
            "target": target,
            "strategy": resolved.strategy,
            "url": page.url,
            "title": await page.title(),
        }
        self._publish("click", profile, **result)
        return result

    async def fill(
        self, profile: str, target: str, text: str, *, tab_id: str | None = None
    ) -> dict[str, Any]:
        page = await self._page(profile, tab_id)
        resolved = await self._resolve(page, target)
        try:
            await resolved.locator.fill(text, timeout=self.settings.action_timeout_ms)
        except PlaywrightError as exc:
            raise AutomationError(f"Fill failed on {target!r}: {exc}") from exc
        result = {
            "action": "fill",
            "target": target,
            "strategy": resolved.strategy,
            "characters": len(text),
        }
        self._publish("fill", profile, **{**result, "text": "***"})
        return result

    async def type_text(
        self,
        profile: str,
        target: str,
        text: str,
        *,
        tab_id: str | None = None,
        delay_ms: int = 25,
    ) -> dict[str, Any]:
        page = await self._page(profile, tab_id)
        resolved = await self._resolve(page, target)
        try:
            await resolved.locator.click(timeout=self.settings.action_timeout_ms)
            await resolved.locator.press_sequentially(
                text, delay=max(0, min(delay_ms, 500))
            )
        except PlaywrightError as exc:
            raise AutomationError(f"Type failed on {target!r}: {exc}") from exc
        result = {
            "action": "type",
            "target": target,
            "strategy": resolved.strategy,
            "characters": len(text),
        }
        self._publish("type", profile, **result)
        return result

    async def press(
        self,
        profile: str,
        key: str,
        *,
        target: str | None = None,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        page = await self._page(profile, tab_id)
        if target:
            resolved = await self._resolve(page, target)
            await resolved.locator.press(key, timeout=self.settings.action_timeout_ms)
            strategy = resolved.strategy
        else:
            await page.keyboard.press(key)
            strategy = "page"
        await self._settle(page)
        result = {"action": "press", "key": key, "target": target, "strategy": strategy}
        self._publish("press", profile, **result)
        return result

    async def select_option(
        self,
        profile: str,
        target: str,
        value: str,
        *,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        page = await self._page(profile, tab_id)
        resolved = await self._resolve(page, target)
        try:
            try:
                selected = await resolved.locator.select_option(value=value)
            except PlaywrightError:
                selected = await resolved.locator.select_option(label=value)
        except PlaywrightError as exc:
            raise AutomationError(
                f"Could not select {value!r} in {target!r}: {exc}"
            ) from exc
        result = {"action": "select", "target": target, "value": value, "selected": selected}
        self._publish("select", profile, **result)
        return result

    async def check(
        self, profile: str, target: str, *, checked: bool = True, tab_id: str | None = None
    ) -> dict[str, Any]:
        page = await self._page(profile, tab_id)
        resolved = await self._resolve(page, target)
        await resolved.locator.set_checked(checked, timeout=self.settings.action_timeout_ms)
        result = {"action": "check", "target": target, "checked": checked}
        self._publish("check", profile, **result)
        return result

    async def hover(
        self, profile: str, target: str, *, tab_id: str | None = None
    ) -> dict[str, Any]:
        page = await self._page(profile, tab_id)
        resolved = await self._resolve(page, target)
        await resolved.locator.hover(timeout=self.settings.action_timeout_ms)
        result = {"action": "hover", "target": target, "strategy": resolved.strategy}
        self._publish("hover", profile, **result)
        return result

    async def scroll(
        self,
        profile: str,
        direction: str = "down",
        *,
        amount: float = 1.0,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        direction = direction.lower()
        if direction not in {"down", "up", "top", "bottom"}:
            raise AutomationError("Scroll direction must be down, up, top or bottom")
        if amount <= 0 or amount > 50:
            raise AutomationError("Scroll amount must be between 0 and 50 viewports")
        page = await self._page(profile, tab_id)
        viewport = await page.evaluate("() => window.innerHeight")
        if direction in {"down", "up"}:
            delta = int(viewport * amount) * (1 if direction == "down" else -1)
            await page.evaluate(
                f"() => window.scrollBy({{ top: {delta}, behavior: 'instant' }})"
            )
        elif direction == "top":
            await page.evaluate("() => window.scrollTo({ top: 0, behavior: 'instant' })")
        else:
            await page.evaluate(
                "() => window.scrollTo({ top: document.documentElement.scrollHeight, "
                "behavior: 'instant' })"
            )
        state = await page.evaluate(
            "() => ({ y: window.scrollY, height: document.documentElement.scrollHeight })"
        )
        result = {"action": "scroll", "direction": direction, "amount": amount, **state}
        self._publish("scroll", profile, **result)
        return result
