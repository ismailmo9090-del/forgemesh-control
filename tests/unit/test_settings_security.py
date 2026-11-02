"""Settings resolution, private permissions and secret handling."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from forgemesh_browser.security.permissions import ensure_private_dir, secure_file, secure_tree
from forgemesh_browser.security.secrets import generate_token, read_or_create_token, redact
from forgemesh_browser.settings import Settings


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FMB_HEADLESS", "false")
    monkeypatch.setenv("FMB_MAX_CONTEXTS", "2")
    monkeypatch.setenv("FMB_DATA_DIR", str(tmp_path / "env-data"))
    settings = Settings()
    assert settings.headless is False
    assert settings.max_contexts == 2
    assert settings.data_dir == tmp_path / "env-data"


def test_settings_paths_derived(settings: Settings) -> None:
    assert settings.profiles_dir == settings.data_dir / "profiles"
    assert settings.token_path == settings.data_dir / "runtime" / "api_token"
    assert settings.database_path == settings.data_dir / "forgemesh.db"


def test_settings_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        Settings(max_contexts=0)
    with pytest.raises(ValueError):
        Settings(api_port=0)
    with pytest.raises(ValueError):
        Settings(browser_channel="not-a-browser")
    with pytest.raises(ValueError):
        Settings(locale="not a locale")
    with pytest.raises(ValueError):
        Settings(log_level="LOUD")


def test_settings_normalises_channel_and_log_level() -> None:
    assert Settings(browser_channel=" CHROME ").browser_channel == "chrome"
    assert Settings(browser_channel="msedge").browser_channel == "msedge"
    assert Settings(browser_channel=None).browser_channel is None
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_ensure_dirs_private(settings: Settings) -> None:
    for directory in (
        settings.data_dir,
        settings.profiles_dir,
        settings.screenshots_dir,
        settings.runtime_dir,
    ):
        assert directory.is_dir()
        assert _mode(directory) == 0o700


def test_permission_helpers(tmp_path: Path) -> None:
    private = ensure_private_dir(tmp_path / "private")
    assert _mode(private) == 0o700

    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    os.chmod(secret, 0o644)
    secure_file(secret)
    assert _mode(secret) == 0o600

    tree = tmp_path / "tree"
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "file").write_text("x", encoding="utf-8")
    secure_tree(tree)
    assert _mode(tree) == 0o700
    assert _mode(tree / "nested") == 0o700
    assert _mode(tree / "nested" / "file") == 0o600


def test_read_or_create_token_is_stable_and_private(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "api_token"
    token = read_or_create_token(path)
    assert len(token) >= 32
    assert _mode(path) == 0o600
    assert read_or_create_token(path) == token


def test_generate_token_unique() -> None:
    assert generate_token() != generate_token()


def test_redact() -> None:
    assert redact("") == ""
    assert redact("short") == "*****"
    assert redact("abcdefghijklmno") == "abcd…lmno"
