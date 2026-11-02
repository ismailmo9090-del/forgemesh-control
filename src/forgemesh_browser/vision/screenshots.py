"""Screenshot service: capture, register as asset, optional OCR."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forgemesh_browser.core.browser_manager import BrowserManager
from forgemesh_browser.media.asset_storage import AssetStore


class ScreenshotService:
    def __init__(self, browser: BrowserManager, store: AssetStore) -> None:
        self.browser = browser
        self.store = store

    async def capture(
        self,
        profile: str,
        *,
        full_page: bool = False,
        selector: str | None = None,
        output: str | Path | None = None,
        tab_id: str | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        info = await self.browser.screenshot(
            profile,
            path=output,
            full_page=full_page,
            selector=selector,
            tab_id=tab_id,
        )
        if record:
            asset = await self.store.record_reference(
                Path(info["path"]),
                kind="screenshot",
                source_url=info.get("url"),
                profile_id=profile,
            )
            if asset:
                info["asset_id"] = asset["id"]
        return info
