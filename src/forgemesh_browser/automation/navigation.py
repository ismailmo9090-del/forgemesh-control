"""Navigation helpers layered over BrowserManager."""

from __future__ import annotations

from typing import Any

from forgemesh_browser.core.browser_manager import BrowserManager, TabInfo
from forgemesh_browser.core.event_bus import EventBus


class NavigationEngine:
    def __init__(self, browser: BrowserManager, events: EventBus) -> None:
        self.browser = browser
        self.events = events

    async def open(
        self, profile: str, url: str, *, tab_id: str | None = None
    ) -> dict[str, Any]:
        return await self.browser.goto(profile, url, tab_id=tab_id)

    async def reload(self, profile: str, *, tab_id: str | None = None) -> dict[str, Any]:
        return await self.browser.reload(profile, tab_id=tab_id)

    async def back(self, profile: str, *, tab_id: str | None = None) -> dict[str, Any]:
        return await self.browser.go_back(profile, tab_id=tab_id)

    async def forward(self, profile: str, *, tab_id: str | None = None) -> dict[str, Any]:
        return await self.browser.go_forward(profile, tab_id=tab_id)

    async def new_tab(self, profile: str, url: str | None = None) -> TabInfo:
        return await self.browser.new_tab(profile, url)

    async def close_tab(self, profile: str, tab_id: str | None = None) -> None:
        await self.browser.close_tab(profile, tab_id)

    async def tabs(self, profile: str | None = None) -> list[TabInfo]:
        return await self.browser.tabs(profile)
