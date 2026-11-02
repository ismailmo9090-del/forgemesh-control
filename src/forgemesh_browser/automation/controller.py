"""Facade combining navigation, interaction, inspection and workflows."""

from __future__ import annotations

from typing import Any

from forgemesh_browser.automation import accessibility, waits
from forgemesh_browser.automation.interaction import InteractionEngine
from forgemesh_browser.automation.navigation import NavigationEngine
from forgemesh_browser.automation.workflows import WorkflowSpec, run_workflow
from forgemesh_browser.core.browser_manager import BrowserManager
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.settings import Settings


class AutomationController:
    def __init__(
        self, settings: Settings, browser: BrowserManager, events: EventBus
    ) -> None:
        self.settings = settings
        self.browser = browser
        self.events = events
        self.navigation = NavigationEngine(browser, events)
        self.interaction = InteractionEngine(settings, browser, events)

    async def click(self, profile: str, target: str, **kwargs) -> dict[str, Any]:
        return await self.interaction.click(profile, target, **kwargs)

    async def fill(self, profile: str, target: str, text: str, **kwargs) -> dict[str, Any]:
        return await self.interaction.fill(profile, target, text, **kwargs)

    async def type_text(
        self, profile: str, target: str, text: str, **kwargs
    ) -> dict[str, Any]:
        return await self.interaction.type_text(profile, target, text, **kwargs)

    async def press(self, profile: str, key: str, **kwargs) -> dict[str, Any]:
        return await self.interaction.press(profile, key, **kwargs)

    async def select_option(
        self, profile: str, target: str, value: str, **kwargs
    ) -> dict[str, Any]:
        return await self.interaction.select_option(profile, target, value, **kwargs)

    async def check(self, profile: str, target: str, **kwargs) -> dict[str, Any]:
        return await self.interaction.check(profile, target, **kwargs)

    async def hover(self, profile: str, target: str, **kwargs) -> dict[str, Any]:
        return await self.interaction.hover(profile, target, **kwargs)

    async def scroll(
        self, profile: str, direction: str = "down", **kwargs
    ) -> dict[str, Any]:
        return await self.interaction.scroll(profile, direction, **kwargs)

    async def wait_seconds(self, seconds: float) -> dict[str, Any]:
        return await waits.wait_seconds(seconds)

    async def wait_for_selector(
        self, profile: str, selector: str, *, state: str = "visible", tab_id: str | None = None
    ) -> dict[str, Any]:
        page = await self.browser.page(profile, tab_id)
        return await waits.wait_for_selector(
            page, selector, state=state, timeout_ms=self.settings.action_timeout_ms
        )

    async def wait_for_text(
        self, profile: str, text: str, *, tab_id: str | None = None
    ) -> dict[str, Any]:
        page = await self.browser.page(profile, tab_id)
        return await waits.wait_for_text(
            page, text, timeout_ms=self.settings.action_timeout_ms
        )

    async def inspect(
        self,
        profile: str,
        *,
        interactive_only: bool = True,
        limit: int = 300,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        page = await self.browser.page(profile, tab_id)
        snapshot = await accessibility.snapshot(
            page, interactive_only=interactive_only, limit=limit
        )
        snapshot["profile"] = profile
        snapshot["tab"] = self.browser.tab_id_for(page, profile)
        self.events.publish(
            "automation.inspect",
            profile=profile,
            elements=len(snapshot.get("elements", [])),
        )
        return snapshot

    async def extract(self, profile: str, *, tab_id: str | None = None) -> dict[str, Any]:
        page = await self.browser.page(profile, tab_id)
        data = await accessibility.extract(page)
        data["profile"] = profile
        self.events.publish("automation.extract", profile=profile, url=data.get("url"))
        return data

    async def run_workflow(
        self,
        profile: str,
        spec: WorkflowSpec | dict[str, Any],
        *,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        return await run_workflow(self, profile, spec, tab_id=tab_id)
