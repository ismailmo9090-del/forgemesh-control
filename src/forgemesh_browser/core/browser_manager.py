"""Async Chromium/Chrome runtime with persistent, isolated profile contexts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import BaseModel

from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import (
    BrowserError,
    BrowserLimitError,
    BrowserNotRunning,
    DownloadTimeout,
    MediaError,
    NavigationError,
)
from forgemesh_browser.profiles.storage import ensure_profile_layout
from forgemesh_browser.security.path_validation import sanitize_filename
from forgemesh_browser.settings import Settings

logger = logging.getLogger("forgemesh.browser")

DownloadHandler = Callable[[Path, str, str | None, str], Awaitable[dict[str, Any]]]


class TabInfo(BaseModel):
    id: str
    url: str
    title: str
    is_active: bool
    profile_id: str


@dataclass
class ProfileBrowser:
    profile_id: str
    context: BrowserContext
    headless: bool
    started_at: datetime
    browser: Browser | None = None
    process: subprocess.Popen[bytes] | None = None
    pages: dict[str, Page] = field(default_factory=dict)
    active_tab: str | None = None
    console_logs: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=300))
    page_errors: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=100))
    downloads: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    tasks: set[asyncio.Task] = field(default_factory=set)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise NavigationError("URL cannot be empty")
    if "://" not in url:
        host = url.split("/", 1)[0].split(":", 1)[0]
        scheme = "http" if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} else "https"
        url = f"{scheme}://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise NavigationError("Only http and https URLs are supported")
    if not parsed.netloc:
        raise NavigationError(f"Invalid URL: {url}")
    return url


def resolve_channel(settings: Settings) -> str | None:
    channel = (settings.browser_channel or "").strip().lower()
    if channel in {"", "auto", "none"}:
        if shutil.which("google-chrome") or Path("/usr/bin/google-chrome").exists():
            return "chrome"
        return None
    if channel in {"chromium", "bundled"}:
        return None
    return channel


_chrome_version_cache: str | None = None


def _installed_chrome_version() -> str | None:
    """Best-effort full Chrome version (e.g. ``156.0.8078.79``) for the UA string."""

    global _chrome_version_cache
    if _chrome_version_cache is not None:
        return _chrome_version_cache or None
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/usr/bin/google-chrome",
        "/opt/google/chrome/chrome",
    ]
    cache = Path.home() / ".cache" / "ms-playwright"
    if cache.is_dir():
        for pattern in (
            "chromium-*/chrome-linux*/chrome",
            "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
            "chromium-*/chrome-win*/chrome.exe",
        ):
            candidates.extend(str(path) for path in sorted(cache.glob(pattern), reverse=True))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            output = subprocess.run(  # noqa: S603 - fixed local binary, no shell
                [candidate, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"(\d+(?:\.\d+){1,3})", output.stdout or "")
        if match:
            _chrome_version_cache = match.group(1)
            return _chrome_version_cache
    _chrome_version_cache = ""
    return None


def _platform_token() -> str:
    system = platform.system()
    if system == "Windows":
        return "Windows NT 10.0; Win64; x64"
    if system == "Darwin":
        return "Macintosh; Intel Mac OS X 10_15_7"
    return "X11; Linux x86_64"


def desktop_user_agent() -> str:
    """A real desktop Chrome UA. Avoids the ``HeadlessChrome`` token Google rejects."""

    version = _installed_chrome_version()
    major = version.split(".")[0] if version else "140"
    # Desktop Chrome reports a reduced UA version (major.0.0.0), not the full build.
    return (
        f"Mozilla/5.0 ({_platform_token()}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


#: Removes the automation markers Google/Cloudflare inspect before allowing OAuth.
#: This only hides Playwright's own flags; credentials, CAPTCHA and MFA still
#: have to be completed by the human in the visible window.
STEALTH_INIT_SCRIPT = """
(() => {
  const define = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, { get: () => value, configurable: true });
    } catch (err) { /* ignore */ }
  };
  define(Navigator.prototype, 'webdriver', false);
  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', {
      value: { runtime: {}, app: { isInstalled: false }, csi: () => {}, loadTimes: () => {} },
      configurable: true,
    });
  }
  if (navigator.userAgentData && !navigator.userAgentData.brands.some(b => b.brand === 'Google Chrome')) {
    const brands = navigator.userAgentData.brands.filter(b => b.brand !== 'Chromium');
    define(NavigatorUAData.prototype, 'brands', brands.concat([{ brand: 'Google Chrome', version: '1' }]));
  }
})();
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
            logger.warning("Chrome process %s did not exit after SIGKILL", process.pid)


def _profile_process_pids(
    user_data_dir: Path, *, proc_root: Path = Path("/proc")
) -> list[int]:
    """PIDs of Chrome processes using ``user_data_dir`` (Linux /proc scan).

    Chrome rewrites its process title on Linux, so ``/proc/<pid>/cmdline`` is
    frequently a single space-separated string instead of NUL-separated argv.
    Both forms are accepted.
    """

    needle = f"--user-data-dir={user_data_dir}".encode()
    pids: list[int] = []
    if not proc_root.is_dir():
        return pids
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        text = raw.replace(b"\x00", b" ").rstrip()
        if b"chrome" not in text.lower():
            continue
        if text.endswith(needle) or needle + b" " in text:
            pids.append(int(entry.name))
    return sorted(pids)


def _clear_singleton_files(profile_dir: Path) -> None:
    """Remove profile locks left behind by a killed Chrome process."""

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_dir / name).unlink()
        except OSError:
            pass


class BrowserManager:
    def __init__(self, settings: Settings, events: EventBus) -> None:
        self.settings = settings
        self.events = events
        self._playwright: Playwright | None = None
        self._profiles: dict[str, ProfileBrowser] = {}
        self._start_lock = asyncio.Lock()
        self._stop_locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._download_handler: DownloadHandler | None = None
        self._closed = False

    def _stop_lock(self, profile_id: str) -> asyncio.Lock:
        lock = self._stop_locks.get(profile_id)
        if lock is None:
            lock = self._stop_locks.setdefault(profile_id, asyncio.Lock())
        return lock

    def _spawn_background(self, awaitable: Any) -> None:
        """Run blocking cleanup off the event loop without leaking the task."""

        task = asyncio.create_task(awaitable)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------ setup
    def set_download_handler(self, handler: DownloadHandler) -> None:
        self._download_handler = handler

    async def _ensure_playwright(self) -> Playwright:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    async def reap_orphans(self) -> int:
        """Terminate Chrome processes left over from a previous daemon.

        A hard daemon crash (SIGKILL, OOM) can leave a Chrome child holding the
        profile's ``SingletonLock`` and debugging port, which would make the
        next launch fail or silently attach to the stale browser.
        """

        root = self.settings.profiles_dir
        if not root.is_dir():
            return 0
        reaped = 0
        for profile_dir in root.iterdir():
            if not profile_dir.is_dir() or profile_dir.name in self._profiles:
                continue
            pids = await asyncio.to_thread(_profile_process_pids, profile_dir)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                    reaped += 1
                except OSError:
                    continue
            if pids:
                await asyncio.sleep(0.5)
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            _clear_singleton_files(profile_dir)
        if reaped:
            logger.warning("Reaped %d orphaned Chrome process(es)", reaped)
            self.events.publish("browser.orphans_reaped", count=reaped)
        return reaped

    async def _open_context(
        self,
        playwright: Playwright,
        *,
        user_data_dir: Path,
        headless: bool,
        channel: str | None,
    ) -> tuple[BrowserContext, Browser | None, subprocess.Popen[bytes] | None]:
        """Open a persistent context.

        Chrome is started as a plain OS process and Playwright attaches over CDP.
        ``launch_persistent_context`` leaves automation markers that make Google
        and other identity providers reject interactive OAuth with
        "This browser or app may not be secure"; a normal process launch is not
        flagged. Non-Chrome channels keep Playwright's own launcher.
        """

        executable = self._chrome_executable(playwright, channel)
        if executable is None:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel=channel,
                headless=headless,
                viewport={
                    "width": self.settings.viewport_width,
                    "height": self.settings.viewport_height,
                },
                accept_downloads=True,
                downloads_path=str(self.settings.downloads_dir),
                locale=self.settings.locale,
                user_agent=desktop_user_agent() if headless else None,
                ignore_default_args=["--enable-automation"],
                timeout=self.settings.navigation_timeout_ms,
                args=[
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-dev-shm-usage",
                    "--password-store=basic",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            return context, None, None

        last_error: Exception | None = None
        for _attempt in range(3):
            port = _free_port()
            process = subprocess.Popen(  # noqa: S603 - fixed executable, no shell
                [executable, *self._chrome_args(user_data_dir, port, headless)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                await self._wait_for_cdp(port, process)
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}"
                )
            except (BrowserError, PlaywrightError) as exc:
                last_error = exc
                await asyncio.to_thread(_terminate, process)
                continue
            contexts = browser.contexts
            if not contexts:
                await asyncio.to_thread(_terminate, process)
                raise BrowserError(
                    f"Chrome for profile '{user_data_dir.name}' exposed no browser context"
                )
            return contexts[0], browser, process
        raise BrowserError(
            f"Could not start Chrome for profile '{user_data_dir.name}': {last_error}"
        ) from last_error

    def _chrome_executable(self, playwright: Playwright, channel: str | None) -> str | None:
        if channel == "chrome":
            for candidate in (
                shutil.which("google-chrome"),
                shutil.which("google-chrome-stable"),
                "/usr/bin/google-chrome",
                "/opt/google/chrome/chrome",
            ):
                if candidate and Path(candidate).exists():
                    return candidate
            return None
        if channel is None:
            bundled = playwright.chromium.executable_path
            return bundled if bundled and Path(bundled).exists() else None
        return None

    def _chrome_args(self, user_data_dir: Path, port: int, headless: bool) -> list[str]:
        args = [
            f"--user-data-dir={user_data_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            f"--window-size={self.settings.viewport_width},{self.settings.viewport_height}",
            f"--lang={self.settings.locale}",
            "about:blank",
        ]
        if headless:
            args.insert(0, "--headless=new")
            # New headless Chrome advertises "HeadlessChrome" in its UA, which
            # identity providers reject. Present the normal desktop UA instead.
            args.append(f"--user-agent={desktop_user_agent()}")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(0, "--no-sandbox")
        return args

    async def _wait_for_cdp(
        self, port: int, process: subprocess.Popen[bytes], timeout: float = 30.0
    ) -> None:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{port}/json/version"
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise BrowserError(
                        f"Chrome exited during startup (code {process.returncode})"
                    )
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.15)
        raise BrowserError(f"Chrome debugging port {port} did not open within {timeout:.0f}s")

    async def start(
        self, profile_id: str, *, headless: bool | None = None
    ) -> ProfileBrowser:
        async with self._start_lock:
            existing = self._profiles.get(profile_id)
            if existing is not None:
                if self._context_alive(existing):
                    return existing
                # A window the user closed (or a crashed renderer) leaves the
                # registered context dead; rebuilding is the only way back.
                logger.warning(
                    "Profile %s has a closed browser context; starting it again",
                    profile_id,
                )
                with contextlib.suppress(Exception):
                    await self.stop(profile_id)
                self._profiles.pop(profile_id, None)
            if len(self._profiles) >= self.settings.max_contexts:
                raise BrowserLimitError(
                    f"Maximum concurrent profiles reached ({self.settings.max_contexts}). "
                    "Stop a profile first: fmb stop --profile <id>"
                )
            if self._closed:
                raise BrowserError("Browser manager is shutting down")

            self.settings.ensure_dirs()
            user_data_dir = ensure_profile_layout(self.settings, profile_id)
            playwright = await self._ensure_playwright()
            # Probing installed Chrome binaries for the UA string runs
            # subprocesses with a multi-second timeout; keep that off the loop.
            await asyncio.to_thread(desktop_user_agent)
            channel = resolve_channel(self.settings)
            headless_mode = self.settings.headless if headless is None else headless

            self.events.publish(
                "browser.starting", profile=profile_id, headless=headless_mode, channel=channel
            )
            browser: Browser | None = None
            process: subprocess.Popen[bytes] | None = None
            try:
                context, browser, process = await self._open_context(
                    playwright,
                    user_data_dir=user_data_dir,
                    headless=headless_mode,
                    channel=channel,
                )
            except PlaywrightError as exc:
                raise BrowserError(
                    f"Could not start browser for profile '{profile_id}': {exc}. "
                    "Run scripts/setup_browser.sh or set FMB_BROWSER_CHANNEL=chromium."
                ) from exc

            if self.settings.browser_stealth and browser is None:
                # Only the Playwright launcher needs cosmetic patching; a Chrome
                # started as a normal process already reports clean signals, and
                # patching it would itself look scripted.
                await context.add_init_script(STEALTH_INIT_SCRIPT)

            context.set_default_timeout(self.settings.action_timeout_ms)
            context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)

            profile_browser = ProfileBrowser(
                profile_id=profile_id,
                context=context,
                headless=headless_mode,
                started_at=datetime.now(UTC),
                browser=browser,
                process=process,
            )
            self._profiles[profile_id] = profile_browser
            context.on("page", lambda page: self._register_page(profile_browser, page))

            def forget(*_args: object) -> None:
                self._drop_profile(profile_id, profile_browser)

            context.on("close", forget)
            if browser is not None:
                browser.on("disconnected", forget)

            for page in context.pages:
                self._register_page(profile_browser, page)
            if not profile_browser.pages:
                page = await context.new_page()
                self._register_page(profile_browser, page)

            self.events.publish(
                "browser.started",
                profile=profile_id,
                headless=headless_mode,
                channel=channel,
                tabs=len(profile_browser.pages),
            )
            return profile_browser

    # ------------------------------------------------------------------- tabs
    def _register_page(self, pb: ProfileBrowser, page: Page) -> None:
        if page in pb.pages.values():
            return
        tab_id = uuid.uuid4().hex[:8]
        pb.pages[tab_id] = page
        if pb.active_tab is None:
            pb.active_tab = tab_id

        def on_console(message) -> None:
            entry = {
                "type": message.type,
                "text": message.text,
                "location": message.location,
                "timestamp": datetime.now(UTC).isoformat(),
                "tab": tab_id,
            }
            pb.console_logs.append(entry)
            if message.type in {"error", "warning"}:
                self.events.publish(
                    "browser.console", profile=pb.profile_id, **entry
                )

        def on_page_error(error) -> None:
            entry = {
                "message": str(error),
                "timestamp": datetime.now(UTC).isoformat(),
                "tab": tab_id,
            }
            pb.page_errors.append(entry)
            self.events.publish("browser.page_error", profile=pb.profile_id, **entry)

        def on_download(download: Download) -> None:
            task = asyncio.create_task(self._handle_download(pb, download))
            pb.tasks.add(task)
            task.add_done_callback(pb.tasks.discard)

        def on_close(_page) -> None:
            pb.pages.pop(tab_id, None)
            if pb.active_tab == tab_id:
                pb.active_tab = next(iter(pb.pages), None)
            self.events.publish(
                "browser.page_closed",
                profile=pb.profile_id,
                tab=tab_id,
                remaining=len(pb.pages),
            )

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("download", on_download)
        page.on("close", on_close)
        self.events.publish(
            "browser.page_opened", profile=pb.profile_id, tab=tab_id, url="about:blank"
        )

    @staticmethod
    def _context_alive(pb: ProfileBrowser) -> bool:
        """Whether the registered context still accepts commands.

        Playwright only reports a closed context when something is asked of it,
        so this checks the browser's own connection first and falls back to a
        cheap read of ``pages`` for contexts that have no Browser handle.
        """
        if pb.browser is not None:
            try:
                return pb.browser.is_connected()
            except PlaywrightError:
                return False
        try:
            _ = pb.context.pages
        except PlaywrightError:
            return False
        return True

    def _drop_profile(self, profile_id: str, pb: ProfileBrowser) -> None:
        """Forget a profile whose window was closed underneath the runtime.

        Playwright raises only when the next command touches the dead context,
        which turned a closed window into an unexplained 500 mid-conversation.
        Dropping the registration means the next call starts a fresh browser.
        """
        if self._profiles.get(profile_id) is not pb:
            return
        self._profiles.pop(profile_id, None)
        pb.pages.clear()
        pb.active_tab = None
        # A context can close while its Chrome process keeps the profile
        # directory locked; leaving it running makes the next start fail with
        # "Chrome exited during startup (code 21)". The wait can take seconds,
        # so it must never run inline inside this Playwright callback.
        if pb.process is not None and pb.process.poll() is None:
            self._spawn_background(asyncio.to_thread(_terminate, pb.process))
        logger.info("Profile %s closed; it will be started again on demand", profile_id)
        self.events.publish("browser.closed", profile=profile_id)

    async def _new_page(self, pb: ProfileBrowser) -> tuple[ProfileBrowser, Page]:
        """Create a tab, rebuilding the profile if its context died meanwhile."""
        try:
            page = await pb.context.new_page()
        except PlaywrightError:
            logger.warning(
                "Profile %s lost its browser while opening a tab; restarting it",
                pb.profile_id,
            )
            pb = await self.start(pb.profile_id, headless=pb.headless)
            page = await pb.context.new_page()
        self._register_page(pb, page)
        return pb, page

    async def page(
        self, profile_id: str, tab_id: str | None = None, *, auto_start: bool = True
    ) -> Page:
        pb = self._profiles.get(profile_id)
        if pb is not None and not self._context_alive(pb):
            pb = None
        if pb is None:
            if not auto_start:
                raise BrowserNotRunning(f"Profile '{profile_id}' is not running")
            pb = await self.start(profile_id)

        if tab_id:
            page = pb.pages.get(tab_id)
            if page is None or page.is_closed():
                raise BrowserNotRunning(f"Tab '{tab_id}' is not open")
            pb.active_tab = tab_id
            return page

        if pb.active_tab and pb.active_tab in pb.pages:
            page = pb.pages[pb.active_tab]
            if not page.is_closed():
                return page
            pb.pages.pop(pb.active_tab, None)
        if pb.pages:
            pb.active_tab = next(iter(pb.pages))
            return pb.pages[pb.active_tab]
        _live, page = await self._new_page(pb)
        return page

    async def newest_page(self, profile_id: str) -> Page:
        """Return the most recently opened tab and make it the active one.

        Sign-in flows often open a popup (Google, X). After clicking the site's
        login button the runner has to keep working in *that* tab, not the
        original one.
        """

        pb = self._profiles.get(profile_id)
        if pb is None:
            raise BrowserNotRunning(f"Profile '{profile_id}' is not running")
        if not self._context_alive(pb):
            pb = await self.start(profile_id, headless=pb.headless)
        live = [
            (tab_id, page)
            for tab_id, page in pb.pages.items()
            if not page.is_closed()
        ]
        if not live:
            fresh, page = await self._new_page(pb)
            fresh.active_tab = next(
                key for key, value in fresh.pages.items() if value is page
            )
            return page
        tab_id, page = live[-1]
        pb.active_tab = tab_id
        return page

    async def tabs(self, profile_id: str | None = None) -> list[TabInfo]:
        profile_ids = [profile_id] if profile_id else list(self._profiles)
        infos: list[TabInfo] = []
        for pid in profile_ids:
            pb = self._profiles.get(pid)
            if pb is None:
                continue
            for tab_id, page in list(pb.pages.items()):
                if page.is_closed():
                    pb.pages.pop(tab_id, None)
                    continue
                try:
                    title = await page.title()
                    url = page.url
                except PlaywrightError:
                    title, url = "", ""
                infos.append(
                    TabInfo(
                        id=tab_id,
                        url=url,
                        title=title,
                        is_active=tab_id == pb.active_tab,
                        profile_id=pid,
                    )
                )
        return infos

    def activate_tab(self, profile_id: str, tab_id: str) -> None:
        pb = self._profiles.get(profile_id)
        if pb is None or tab_id not in pb.pages:
            raise BrowserNotRunning(f"Tab '{tab_id}' is not open in profile '{profile_id}'")
        pb.active_tab = tab_id

    async def new_tab(self, profile_id: str, url: str | None = None) -> TabInfo:
        pb = await self.start(profile_id)
        # The context may have died between start() and here; _new_page rebuilds
        # it and returns the registration it actually used.
        pb, page = await self._new_page(pb)
        tab_id = next(key for key, value in pb.pages.items() if value is page)
        if url:
            await self.goto(profile_id, url, tab_id=tab_id)
        title = ""
        if page.url != "about:blank":
            with contextlib.suppress(PlaywrightError):
                title = await page.title()
        return TabInfo(
            id=tab_id,
            url=page.url,
            title=title,
            is_active=pb.active_tab == tab_id,
            profile_id=profile_id,
        )

    async def close_tab(self, profile_id: str, tab_id: str | None = None) -> None:
        pb = self._profiles.get(profile_id)
        if pb is None:
            raise BrowserNotRunning(f"Profile '{profile_id}' is not running")
        tab_id = tab_id or pb.active_tab
        page = pb.pages.get(tab_id or "")
        if page is None:
            raise BrowserNotRunning(f"Tab '{tab_id}' is not open")
        await page.close()
        if tab_id:
            pb.pages.pop(tab_id, None)
        if pb.active_tab == tab_id:
            pb.active_tab = next(iter(pb.pages), None)

    # -------------------------------------------------------------- navigation
    async def goto(
        self,
        profile_id: str,
        url: str,
        *,
        tab_id: str | None = None,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]:
        target = normalize_url(url)
        page = await self.page(profile_id, tab_id)
        self.events.publish("browser.navigating", profile=profile_id, url=target)
        try:
            response = await page.goto(
                target,
                wait_until=wait_until,  # type: ignore[arg-type]
                timeout=self.settings.navigation_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"Navigation timed out: {target}") from exc
        except PlaywrightError as exc:
            raise NavigationError(f"Navigation failed: {exc}") from exc
        title = await page.title()
        result = {
            "url": page.url,
            "title": title,
            "status": response.status if response else None,
            "profile": profile_id,
            "tab": self.tab_id_for(page, profile_id),
        }
        self.events.publish("browser.loaded", **result)
        return result

    async def reload(self, profile_id: str, *, tab_id: str | None = None) -> dict[str, Any]:
        page = await self.page(profile_id, tab_id)
        await page.reload(wait_until="domcontentloaded")
        return {"url": page.url, "title": await page.title(), "tab": self.tab_id_for(page, profile_id)}

    async def go_back(self, profile_id: str, *, tab_id: str | None = None) -> dict[str, Any]:
        page = await self.page(profile_id, tab_id)
        await page.go_back(wait_until="domcontentloaded")
        return {"url": page.url, "title": await page.title()}

    async def go_forward(self, profile_id: str, *, tab_id: str | None = None) -> dict[str, Any]:
        page = await self.page(profile_id, tab_id)
        await page.go_forward(wait_until="domcontentloaded")
        return {"url": page.url, "title": await page.title()}

    def tab_id_for(self, page: Page, profile_id: str) -> str | None:
        pb = self._profiles.get(profile_id)
        if pb is None:
            return None
        for tab_id, candidate in pb.pages.items():
            if candidate is page:
                return tab_id
        return None

    # ------------------------------------------------------------- screenshots
    async def screenshot(
        self,
        profile_id: str,
        *,
        path: Path | str | None = None,
        full_page: bool = False,
        selector: str | None = None,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        page = await self.page(profile_id, tab_id)
        if path is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = self.settings.screenshots_dir / f"{profile_id}-{stamp}-{uuid.uuid4().hex[:6]}.png"
        else:
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = self.settings.screenshots_dir / target
            target.parent.mkdir(parents=True, exist_ok=True)

        if selector:
            locator = page.locator(selector)
            await locator.first.wait_for(state="visible")
            await locator.first.screenshot(path=str(target))
        else:
            await page.screenshot(path=str(target), full_page=full_page)

        info: dict[str, Any] = {
            "path": str(target),
            "profile": profile_id,
            "url": page.url,
            "full_page": full_page,
            "selector": selector,
        }
        try:
            from PIL import Image

            with Image.open(target) as image:
                info["width"], info["height"] = image.size
                info["format"] = image.format
        except Exception:  # pragma: no cover - image inspection is best-effort
            pass
        info["size"] = target.stat().st_size
        self.events.publish("browser.screenshot", profile=profile_id, path=str(target))
        return info

    # ------------------------------------------------------------------ files
    async def upload(
        self,
        profile_id: str,
        path: Path | str,
        *,
        selector: str | None = None,
        tab_id: str | None = None,
    ) -> dict[str, Any]:
        file_path = Path(path)
        page = await self.page(profile_id, tab_id)
        if selector:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible")
            tag = await locator.evaluate("el => el.tagName.toLowerCase()")
            if tag == "input":
                await locator.set_input_files(str(file_path))
            else:
                async with page.expect_file_chooser() as chooser_info:
                    await locator.click()
                chooser = await chooser_info.value
                await chooser.set_files(str(file_path))
        else:
            file_inputs = page.locator("input[type=file]")
            count = await file_inputs.count()
            if count == 0:
                raise MediaError(
                    "No file input on this page. Use --selector for a button that opens a file chooser."
                )
            if count > 1:
                raise MediaError(
                    f"{count} file inputs found. Choose one with --selector."
                )
            await file_inputs.first.set_input_files(str(file_path))
        self.events.publish(
            "browser.upload", profile=profile_id, path=str(file_path), selector=selector
        )
        return {
            "uploaded": str(file_path),
            "name": file_path.name,
            "size": file_path.stat().st_size,
            "selector": selector,
        }

    async def _handle_download(self, pb: ProfileBrowser, download: Download) -> None:
        try:
            self.settings.ensure_dirs()
            suggested = sanitize_filename(download.suggested_filename, "download")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = self.settings.downloads_dir / f"{stamp}-{uuid.uuid4().hex[:6]}-{suggested}"
            await download.save_as(target)
            failure = await download.failure()
            if failure:
                target.unlink(missing_ok=True)
                raise MediaError(f"Download failed: {failure}")
            size = target.stat().st_size
            if size > self.settings.download_max_bytes:
                target.unlink(missing_ok=True)
                raise MediaError(
                    f"Download exceeded limit ({size} > {self.settings.download_max_bytes} bytes)"
                )
            info: dict[str, Any] = {
                "path": str(target),
                "suggested_filename": suggested,
                "size": size,
                "url": download.url,
                "profile": pb.profile_id,
            }
            if self._download_handler is not None:
                info = await self._download_handler(
                    target, download.url, pb.profile_id, suggested
                )
            pb.downloads.put_nowait(info)
            self.events.publish("browser.download", **info)
        except Exception as exc:
            logger.exception("Download handling failed")
            pb.downloads.put_nowait({"error": str(exc), "profile": pb.profile_id})
            self.events.publish("browser.download_failed", profile=pb.profile_id, error=str(exc))

    async def wait_for_download(
        self, profile_id: str, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        pb = self._profiles.get(profile_id)
        if pb is None:
            raise BrowserNotRunning(f"Profile '{profile_id}' is not running")
        try:
            info = await asyncio.wait_for(pb.downloads.get(), timeout=timeout)
        except TimeoutError as exc:
            raise DownloadTimeout(
                f"No download started within {timeout:.0f}s on profile '{profile_id}'"
            ) from exc
        if "error" in info:
            raise MediaError(info["error"])
        return info

    # ------------------------------------------------------------------ status
    def is_running(self, profile_id: str) -> bool:
        return profile_id in self._profiles

    def headless_mode(self, profile_id: str) -> bool | None:
        pb = self._profiles.get(profile_id)
        return None if pb is None else pb.headless

    def running_profiles(self) -> list[str]:
        return list(self._profiles)

    def console_logs(self, profile_id: str, limit: int = 100) -> list[dict[str, Any]]:
        pb = self._profiles.get(profile_id)
        if pb is None:
            return []
        return list(pb.console_logs)[-limit:]

    def errors(self, profile_id: str, limit: int = 50) -> list[dict[str, Any]]:
        pb = self._profiles.get(profile_id)
        if pb is None:
            return []
        return list(pb.page_errors)[-limit:]

    async def status(self) -> dict[str, Any]:
        profiles = []
        # A provider can drop its own profile while the loop awaits tabs();
        # iterate a snapshot so /status never dies with "dictionary changed
        # size during iteration".
        for profile_id, pb in list(self._profiles.items()):
            tabs = await self.tabs(profile_id)
            profiles.append(
                {
                    "profile": profile_id,
                    "headless": pb.headless,
                    "started_at": pb.started_at.isoformat(),
                    "tabs": len(tabs),
                    "active_tab": pb.active_tab,
                    "url": next(
                        (tab.url for tab in tabs if tab.id == pb.active_tab), None
                    ),
                    "console_errors": sum(
                        1 for log in pb.console_logs if log.get("type") == "error"
                    ),
                }
            )
        return {
            "running": bool(self._profiles),
            "profiles": profiles,
            "channel": resolve_channel(self.settings),
            "max_contexts": self.settings.max_contexts,
        }

    # ------------------------------------------------------------------- stop
    async def stop(self, profile_id: str) -> bool:
        """Close one profile and only then unregister it.

        Keeping the registration until the browser is really gone stops a
        concurrent start() from launching a second Chrome on the same profile
        directory, which Chrome rejects with "exited during startup (code 21)".
        """

        async with self._stop_lock(profile_id):
            pb = self._profiles.get(profile_id)
            if pb is None:
                return False
            for task in list(pb.tasks):
                task.cancel()
            if pb.browser is not None:
                try:
                    session = await pb.browser.new_browser_cdp_session()
                    await session.send("Browser.close")
                except PlaywrightError:
                    logger.debug("CDP browser already gone for profile %s", profile_id)
                try:
                    await pb.browser.close()
                except PlaywrightError:  # pragma: no cover - connection already closed
                    logger.debug("CDP connection already closed for profile %s", profile_id)
                if pb.process is not None:
                    try:
                        await asyncio.to_thread(pb.process.wait, 10)
                    except subprocess.TimeoutExpired:
                        await asyncio.to_thread(_terminate, pb.process)
            else:
                try:
                    await pb.context.close()
                except PlaywrightError:  # pragma: no cover - browser may already be gone
                    logger.debug("Context already closed for profile %s", profile_id)
            if self._profiles.get(profile_id) is pb:
                self._profiles.pop(profile_id, None)
            self.events.publish("browser.stopped", profile=profile_id)
            return True

    async def stop_all(self) -> None:
        for profile_id in list(self._profiles):
            await self.stop(profile_id)

    async def close(self) -> None:
        self._closed = True
        await self.stop_all()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
