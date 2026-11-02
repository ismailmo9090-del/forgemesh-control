"""High-level session façade used by CLI and API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forgemesh_browser.core.browser_manager import BrowserManager
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import BrowserNotRunning
from forgemesh_browser.profiles.manager import ProfileManager
from forgemesh_browser.settings import Settings
from forgemesh_browser.vision.screenshots import ScreenshotService


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        browser: BrowserManager,
        profiles: ProfileManager,
        screenshots: ScreenshotService,
        events: EventBus,
    ) -> None:
        self.settings = settings
        self.browser = browser
        self.profiles = profiles
        self.screenshots = screenshots
        self.events = events

    async def start(
        self, profile: str, *, headless: bool | None = None
    ) -> dict[str, Any]:
        await self.profiles.require(profile)
        pb = await self.browser.start(profile, headless=headless)
        await self.profiles.touch(profile)
        return {
            "profile": profile,
            "headless": pb.headless,
            "tabs": len(pb.pages),
            "pid_note": "managed by the local daemon",
        }

    async def open(
        self,
        profile: str,
        url: str,
        *,
        headless: bool | None = None,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        await self.profiles.require(profile)
        await self.browser.start(profile, headless=headless)
        info = await self.browser.goto(profile, url, tab_id=tab_id)
        await self.profiles.touch(profile)
        return info

    async def stop(self, profile: str) -> bool:
        stopped = await self.browser.stop(profile)
        if not stopped:
            raise BrowserNotRunning(f"Profile '{profile}' is not running")
        return True

    async def stop_all(self) -> int:
        count = len(self.browser.running_profiles())
        await self.browser.stop_all()
        return count

    async def tabs(self, profile: str | None = None) -> list[dict[str, Any]]:
        return [tab.model_dump() for tab in await self.browser.tabs(profile)]

    async def screenshot(
        self,
        profile: str,
        *,
        full_page: bool = False,
        selector: str | None = None,
        output: str | Path | None = None,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        info = await self.screenshots.capture(
            profile,
            full_page=full_page,
            selector=selector,
            output=output,
            tab_id=tab_id,
        )
        await self.profiles.touch(profile)
        return info

    async def status(self) -> dict[str, Any]:
        browser_status = await self.browser.status()
        return {
            "running": browser_status["running"],
            "selected_profile": self.profiles.current(),
            "channel": browser_status["channel"],
            "max_contexts": browser_status["max_contexts"],
            "profiles": browser_status["profiles"],
        }

    def running(self, profile: str) -> bool:
        return self.browser.is_running(profile)
