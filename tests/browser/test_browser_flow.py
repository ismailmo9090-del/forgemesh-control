"""End-to-end browser behaviour against the local mock site (needs Chrome)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from forgemesh_browser.core.exceptions import (
    AutomationError,
    BrowserLimitError,
    BrowserNotRunning,
)
from forgemesh_browser.core.runtime import Runtime

pytestmark = pytest.mark.browser


async def test_navigation_inspection_and_extraction(
    browser_runtime: Runtime, site_url: str
) -> None:
    runtime = browser_runtime
    loaded = await runtime.sessions.open("primary", f"{site_url}/index.html")
    assert loaded["title"] == "FMB Test Page"
    assert runtime.browser.headless_mode("primary") is True

    snapshot = await runtime.automation.inspect("primary", interactive_only=False)
    elements = snapshot["elements"]
    by_id = {item["id"]: item for item in elements if item["id"]}
    assert by_id["password"]["has_value"] is True
    assert "value" not in by_id["password"]
    assert by_id["email"]["name"] == "Email address"
    assert by_id["status"]["text"] == "idle"

    serialized = json.dumps(snapshot)
    assert "secret-value" not in serialized

    extracted = await runtime.automation.extract("primary")
    assert extracted["title"] == "FMB Test Page"
    assert "ForgeMesh Test Page" in [item["text"] for item in extracted["headings"]]
    assert extracted["forms"]
    assert extracted["description"] == "Local page for ForgeMesh tests"
    assert all("secret-value" not in json.dumps(form) for form in extracted["forms"])


async def test_interactions_screenshot_and_console(
    browser_runtime: Runtime, site_url: str
) -> None:
    runtime = browser_runtime
    await runtime.sessions.open("primary", f"{site_url}/index.html")
    page = await runtime.browser.page("primary")

    filled = await runtime.automation.fill("primary", "label=Email address", "user@example.com")
    assert filled["strategy"] == "label=Email address"
    assert await page.input_value("#email") == "user@example.com"

    history = runtime.events.history(limit=200)
    fill_events = [
        event
        for event in history
        if event["type"] == "automation.action" and event["data"].get("action") == "fill"
    ]
    assert fill_events and all(event["data"]["text"] == "***" for event in fill_events)
    assert "user@example.com" not in json.dumps(history)

    clicked = await runtime.automation.click("primary", 'role=button[name="Sign in"]')
    assert clicked["strategy"].startswith("role=button")
    assert await page.inner_text("#status") == "clicked"

    await runtime.automation.select_option("primary", "#pick", "b")
    assert await page.input_value("#pick") == "b"

    await runtime.automation.check("primary", "#agree")
    assert await page.is_checked("#agree") is True

    await runtime.automation.scroll("primary", "down", amount=0.8)
    assert await page.evaluate("() => window.scrollY") > 0

    shot = await runtime.sessions.screenshot("primary")
    assert Path(shot["path"]).is_file()
    assert shot["width"] > 0 and shot["height"] > 0
    assert shot["asset_id"]

    full = await runtime.sessions.screenshot("primary", full_page=True)
    assert full["height"] > shot["height"]

    console = runtime.browser.console_logs("primary")
    assert any("fmb test warning" in entry["text"] for entry in console)

    workflow = await runtime.automation.run_workflow(
        "primary",
        {
            "name": "smoke",
            "steps": [
                {"action": "fill", "target": "#search", "text": "hello"},
                {"action": "press", "key": "Enter", "target": "#search"},
                {"action": "wait", "seconds": 0.1},
            ],
        },
        tab_id=None,
    )
    assert workflow["status"] == "completed"
    assert len(workflow["steps"]) == 3

    with pytest.raises(AutomationError) as info:
        await runtime.automation.run_workflow(
            "primary",
            {"name": "bad", "steps": [{"action": "evaluate_js", "text": "alert(1)"}]},
        )
    assert "not allowed" in str(info.value)


async def test_tabs_upload_and_download(browser_runtime: Runtime, site_url: str) -> None:
    runtime = browser_runtime
    await runtime.sessions.open("primary", f"{site_url}/index.html")

    tab = await runtime.browser.new_tab("primary", f"{site_url}/storage.html")
    tabs = await runtime.browser.tabs("primary")
    assert len(tabs) == 2
    assert any(item.id == tab.id for item in tabs)

    runtime.browser.activate_tab("primary", tab.id)
    assert any(
        item.is_active and item.id == tab.id
        for item in await runtime.browser.tabs("primary")
    )
    await runtime.browser.close_tab("primary", tab.id)
    assert len(await runtime.browser.tabs("primary")) == 1

    page = await runtime.browser.page("primary")
    upload_file = runtime.settings.data_dir / "upload.txt"
    upload_file.write_text("upload payload", encoding="utf-8")
    uploaded = await runtime.browser.upload("primary", upload_file, selector="#file")
    assert uploaded["name"] == "upload.txt"
    assert uploaded["size"] == len("upload payload")

    await page.click("#dl")
    download = await runtime.browser.wait_for_download("primary", timeout=15)
    assert download["size"] == len("sample download payload\n")
    assert download["sha256"] == hashlib.sha256(b"sample download payload\n").hexdigest()
    downloaded = Path(download["path"])
    assert downloaded.is_file()
    assert downloaded.read_text(encoding="utf-8") == "sample download payload\n"

    assets = await runtime.asset_store.list(kind="download")
    assert any(item["sha256"] == download["sha256"] for item in assets)


async def test_profile_isolation_and_persistence(
    browser_runtime: Runtime, site_url: str
) -> None:
    runtime = browser_runtime
    await runtime.profiles.create("secondary")

    await runtime.sessions.open("primary", f"{site_url}/storage.html?set=alpha")
    primary_page = await runtime.browser.page("primary")
    assert await primary_page.inner_text("#v") == "mark=alpha"

    await runtime.sessions.open("secondary", f"{site_url}/storage.html")
    secondary_page = await runtime.browser.page("secondary")
    assert await secondary_page.inner_text("#v") == "mark=none"

    await runtime.sessions.stop("primary")
    await runtime.sessions.open("primary", f"{site_url}/storage.html")
    reopened = await runtime.browser.page("primary")
    assert await reopened.inner_text("#v") == "mark=alpha"

    report = await runtime.profiles.inspect("primary")
    assert report["isolation"]["within_root"] is True
    assert report["isolation"]["private"] is True
    assert report["running"] is True


async def test_browser_hides_automation_markers(
    browser_runtime: Runtime, site_url: str
) -> None:
    runtime = browser_runtime
    await runtime.sessions.open("primary", f"{site_url}/index.html")
    page = await runtime.browser.page("primary")
    assert await page.evaluate("navigator.webdriver") is False
    agent = await page.evaluate("navigator.userAgent")
    assert "HeadlessChrome" not in agent
    assert "Chrome/" in agent


async def test_context_limit_and_clean_shutdown(
    browser_runtime: Runtime, site_url: str
) -> None:
    runtime = browser_runtime
    await runtime.profiles.create("limit-check")
    runtime.settings.max_contexts = 1

    await runtime.sessions.open("primary", f"{site_url}/index.html")
    with pytest.raises(BrowserLimitError):
        await runtime.sessions.start("limit-check")

    stopped = await runtime.sessions.stop_all()
    assert stopped == 1
    assert runtime.browser.running_profiles() == []
    with pytest.raises(BrowserNotRunning):
        await runtime.browser.page("primary", auto_start=False)
