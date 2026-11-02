"""Shared pytest fixtures: temp settings, local mock site and browser runtime."""

from __future__ import annotations

import functools
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from forgemesh_browser.core.runtime import Runtime
from forgemesh_browser.settings import Settings

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FMB Test Page</title>
<meta name="description" content="Local page for ForgeMesh tests">
</head>
<body>
<h1>ForgeMesh Test Page</h1>
<p id="status">idle</p>
<form id="login" action="/submit" method="post">
<button id="greet" type="button" onclick="document.getElementById('status').textContent='clicked'">Sign in</button>
<label for="email">Email address</label>
<input id="email" name="email" type="email" placeholder="you@example.com">
<label for="password">Password</label>
<input id="password" name="password" type="password" value="secret-value">
<input id="search" name="q" placeholder="Search query">
<select id="pick" name="pick"><option value="a">Alpha</option><option value="b">Beta</option></select>
<input id="agree" name="agree" type="checkbox">
</form>
<a id="dl" href="sample.txt" download>Download sample</a>
<input id="file" type="file">
<div style="height:1600px"></div>
<p id="bottom">Bottom text</p>
<script>console.warn('fmb test warning');</script>
</body>
</html>
"""

STORAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Storage Isolation</title></head>
<body><h1>Storage Isolation</h1><p id="v">pending</p>
<script>
const params = new URLSearchParams(location.search);
if (params.get('set')) localStorage.setItem('mark', params.get('set'));
document.getElementById('v').textContent = 'mark=' + (localStorage.getItem('mark') || 'none');
</script></body></html>
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: D102 - silence test server
        return


@pytest.fixture(scope="session")
def site_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    root = tmp_path_factory.mktemp("site")
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "storage.html").write_text(STORAGE_HTML, encoding="utf-8")
    (root / "sample.txt").write_text("sample download payload\n", encoding="utf-8")
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    resolved = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        headless=True,
        browser_channel="auto",
        api_port=8765,
        ai_timeout_seconds=5,
    )
    resolved.ensure_dirs()
    return resolved


@pytest.fixture(scope="session")
def chrome_available() -> None:
    if not any(
        shutil.which(name)
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
    ):
        pytest.skip("No Chrome/Chromium installation found")


@pytest.fixture
async def browser_runtime(settings: Settings, chrome_available: None):
    runtime = Runtime(settings)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()
