"""Browser launch hygiene: real UA, no automation flags, stale process recovery."""

from __future__ import annotations

from pathlib import Path

from forgemesh_browser.core.browser_manager import (
    BrowserManager,
    _clear_singleton_files,
    _profile_process_pids,
    desktop_user_agent,
)
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.settings import Settings


def _fake_proc(root: Path, pid: int, args: list[str]) -> None:
    directory = root / str(pid)
    directory.mkdir(parents=True)
    (directory / "cmdline").write_bytes(
        b"\x00".join(part.encode() for part in args) + b"\x00"
    )


def test_desktop_user_agent_is_a_real_chrome() -> None:
    agent = desktop_user_agent()
    assert agent.startswith("Mozilla/5.0 (")
    assert "Chrome/" in agent
    assert "Safari/537.36" in agent
    assert "HeadlessChrome" not in agent
    assert "undefined" not in agent


def test_launch_args_have_no_automation_flags(settings: Settings) -> None:
    manager = BrowserManager(settings, EventBus())
    args = manager._chrome_args(Path("/tmp/profile"), 9333, headless=True)
    assert "--headless=new" in args
    assert f"--remote-debugging-port={9333}" in args
    assert any(arg.startswith("--user-data-dir=") for arg in args)
    assert any(arg.startswith("--user-agent=") for arg in args)
    assert not any("HeadlessChrome" in arg for arg in args)
    assert not any("enable-automation" in arg for arg in args)
    assert not any("AutomationControlled" in arg for arg in args)

    headed = manager._chrome_args(Path("/tmp/profile"), 9333, headless=False)
    assert "--headless=new" not in headed


def test_profile_process_scan_matches_exact_profile(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    profile = tmp_path / "profiles" / "primary"
    _fake_proc(
        proc,
        101,
        ["/usr/bin/google-chrome", f"--user-data-dir={profile}", "about:blank"],
    )
    # Sibling profile with a shared prefix must not match.
    _fake_proc(
        proc,
        102,
        ["/usr/bin/google-chrome", f"--user-data-dir={profile}-extra", "about:blank"],
    )
    # Chrome rewrites its process title into one space-separated command line.
    rewritten = proc / "104"
    rewritten.mkdir(parents=True)
    (rewritten / "cmdline").write_bytes(
        b"/opt/google/chrome/chrome --user-data-dir=%s --remote-debugging-port=9333"
        % str(profile).encode()
    )
    # Unrelated process.
    _fake_proc(proc, 103, ["/usr/bin/python3", "script.py"])

    assert _profile_process_pids(profile, proc_root=proc) == [101, 104]


def test_stale_singleton_files_are_removed(tmp_path: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (tmp_path / name).write_text("stale", encoding="utf-8")
    (tmp_path / "Preferences").write_text("keep", encoding="utf-8")

    _clear_singleton_files(tmp_path)

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert not (tmp_path / name).exists()
    assert (tmp_path / "Preferences").read_text(encoding="utf-8") == "keep"


def test_a_closed_browser_context_is_detected() -> None:
    from types import SimpleNamespace

    from playwright.async_api import Error as PlaywrightError

    class DeadContext:
        @property
        def pages(self):
            raise PlaywrightError(
                "Target page, context or browser has been closed"
            )

    class LiveContext:
        pages: list = []

    live = SimpleNamespace(context=LiveContext(), browser=None)
    dead = SimpleNamespace(context=DeadContext(), browser=None)
    assert BrowserManager._context_alive(live) is True
    assert BrowserManager._context_alive(dead) is False

    class DisconnectedBrowser:
        def is_connected(self) -> bool:
            return False

    class LiveBrowser:
        def is_connected(self) -> bool:
            return True

    assert BrowserManager._context_alive(
        SimpleNamespace(context=DeadContext(), browser=DisconnectedBrowser())
    ) is False
    assert BrowserManager._context_alive(
        SimpleNamespace(context=DeadContext(), browser=LiveBrowser())
    ) is True


async def test_status_survives_a_profile_disappearing_mid_loop(
    settings: Settings,
) -> None:
    """A provider dropping its profile used to crash /status with RuntimeError."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    manager = BrowserManager(settings, EventBus())
    profile = SimpleNamespace(
        headless=True,
        started_at=datetime.now(UTC),
        active_tab="tab",
        console_logs=[],
    )
    manager._profiles = {"one": profile, "two": profile}

    async def tabs(profile_id: str) -> list[object]:
        manager._profiles.pop("two", None)
        return []

    manager.tabs = tabs  # type: ignore[method-assign]
    data = await manager.status()
    assert data["running"] is True
    assert len(data["profiles"]) == 2

