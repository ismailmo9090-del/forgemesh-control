"""Composition root shared by the daemon, CLI helpers and tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from forgemesh_browser.accounts.identities import IdentityManager
from forgemesh_browser.accounts.login_runner import IdentityLoginRunner
from forgemesh_browser.accounts.manager import AccountManager
from forgemesh_browser.ai.router import AIRouter
from forgemesh_browser.automation.controller import AutomationController
from forgemesh_browser.core.browser_manager import BrowserManager
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.session_manager import SessionManager
from forgemesh_browser.core.task_manager import TaskManager
from forgemesh_browser.media.asset_storage import AssetStore
from forgemesh_browser.media.downloads import DownloadManager
from forgemesh_browser.media.uploads import UploadManager
from forgemesh_browser.profiles.manager import ProfileManager
from forgemesh_browser.providers.registry import ProviderRegistry
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.security.secrets import read_or_create_token
from forgemesh_browser.security.vault import Vault
from forgemesh_browser.settings import Settings, load_settings
from forgemesh_browser.storage.database import Database
from forgemesh_browser.vision.screenshots import ScreenshotService


class Runtime:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.events = EventBus()
        self.db = Database(self.settings.database_path)
        self.audit = AuditLogger(self.db, self.settings.logs_dir)
        self.tasks = TaskManager(self.db, self.events)
        self.browser = BrowserManager(self.settings, self.events)
        self.profiles = ProfileManager(
            self.settings, self.db, self.events, self.audit, self.browser.is_running
        )
        self.asset_store = AssetStore(self.settings, self.db, self.events)
        self.downloads = DownloadManager(self.asset_store, self.events)
        self.browser.set_download_handler(self.downloads.handle)
        self.uploads = UploadManager(self.settings)
        self.screenshots = ScreenshotService(self.browser, self.asset_store)
        self.sessions = SessionManager(
            self.settings, self.browser, self.profiles, self.screenshots, self.events
        )
        self.providers = ProviderRegistry(self.settings)
        self.vault = Vault(self.settings)
        self.identities = IdentityManager(
            self.settings,
            self.db,
            self.profiles,
            self.providers,
            self.vault,
            self.events,
            self.audit,
        )
        self.accounts = AccountManager(
            self.settings,
            self.db,
            self.profiles,
            self.browser,
            self.providers,
            self.uploads,
            self.asset_store,
            self.events,
            self.audit,
        )
        self.identity_login = IdentityLoginRunner(
            self.settings, self.identities, self.accounts, self.events, self.audit
        )
        self.ai = AIRouter(self.settings, self.events)
        self.automation = AutomationController(self.settings, self.browser, self.events)
        self.started_at: datetime | None = None
        self.api_token = read_or_create_token(self.settings.token_path)

    async def start(self) -> Runtime:
        self.settings.ensure_dirs()
        await self.browser.reap_orphans()
        await self.db.init()
        await self.profiles.ensure_default()
        self.started_at = datetime.now(UTC)
        self.events.publish(
            "runtime.started",
            data_dir=str(self.settings.data_dir),
            pid=os.getpid(),
        )
        return self

    async def stop(self) -> None:
        await self.browser.close()
        await self.db.close()
        self.events.publish("runtime.stopped", started_at=self.started_at.isoformat() if self.started_at else None)

    async def status(self) -> dict[str, Any]:
        sessions = await self.sessions.status()
        return {
            **sessions,
            "daemon": {
                "running": True,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "pid": os.getpid(),
                "data_dir": str(self.settings.data_dir),
                "headless_default": self.settings.headless,
            },
            "ai_providers": self.ai.list_providers(),
            "subscribers": self.events.subscriber_count,
        }

    async def shutdown(self) -> None:
        await self.stop()

    async def __aenter__(self) -> Runtime:
        return await self.start()

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()


async def run_with_runtime(operation) -> Any:
    runtime = Runtime()
    await runtime.start()
    try:
        return await operation(runtime)
    finally:
        await runtime.stop()
