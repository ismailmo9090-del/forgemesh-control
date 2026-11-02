"""Download completion handling and asset registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.media.asset_storage import AssetStore


class DownloadManager:
    def __init__(self, store: AssetStore, events: EventBus) -> None:
        self.store = store
        self.events = events

    async def handle(
        self,
        path: Path,
        source_url: str,
        profile_id: str | None,
        suggested_filename: str,
    ) -> dict[str, Any]:
        """BrowserManager download hook: move the completed file into assets."""

        return await self.store.store_path(
            path,
            kind="download",
            original_name=suggested_filename,
            source_url=source_url,
            profile_id=profile_id,
            move=True,
        )
