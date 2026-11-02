"""Profile identifiers, disk layout and lifecycle."""

from __future__ import annotations

import stat

import pytest

from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import (
    ProfileError,
    ProfileInUse,
    ProfileNotFound,
    SecurityError,
)
from forgemesh_browser.profiles.manager import ProfileManager
from forgemesh_browser.profiles.storage import (
    AppState,
    profile_path,
    read_state,
    selected_profile,
    validate_profile_id,
    write_state,
)
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database


@pytest.mark.parametrize("value", ["primary", "work-1", "a_b", "x", "0profile"])
def test_validate_profile_id_accepts(value: str) -> None:
    assert validate_profile_id(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "with space", "../evil", "a/b", "profiles", "runtime", ".", ".."],
)
def test_validate_profile_id_rejects(value: str) -> None:
    with pytest.raises(ProfileError):
        validate_profile_id(value)


def test_validate_profile_id_normalises() -> None:
    assert validate_profile_id("  Work  ") == "work"


def test_profile_path_containment(settings: Settings) -> None:
    path = profile_path(settings, "alpha")
    assert path == settings.profiles_dir.resolve() / "alpha"


async def test_profile_lifecycle(settings: Settings) -> None:
    settings.ensure_dirs()
    db = Database(settings.database_path)
    await db.init()
    events = EventBus()
    running: set[str] = set()
    manager = ProfileManager(
        settings, db, events, AuditLogger(db, settings.logs_dir), running.__contains__
    )
    try:
        created = await manager.create("work", name="Work")
        assert created.id == "work"
        assert created.selected is True
        assert (settings.profiles_dir / "work").is_dir()
        assert stat.S_IMODE((settings.profiles_dir / "work").stat().st_mode) == 0o700

        with pytest.raises(ProfileError):
            await manager.create("work")

        await manager.create("second")
        listed = await manager.list()
        assert {info.id for info in listed} == {"work", "second"}
        assert manager.current() == "work"

        selected = await manager.activate("second")
        assert selected.selected is True
        assert selected_profile(settings, "") == "second"

        renamed = await manager.rename("work", "Daily driver")
        assert renamed.name == "Daily driver"

        with pytest.raises(ProfileError):
            await manager.rename("work", "  ")

        with pytest.raises(ProfileError):
            await manager.delete("work", confirm=False)

        running.add("work")
        with pytest.raises(ProfileInUse):
            await manager.delete("work", confirm=True)
        running.discard("work")

        await manager.delete("work", confirm=True)
        with pytest.raises(ProfileNotFound):
            await manager.get("work")
        assert not (settings.profiles_dir / "work").exists()

        audit_file = settings.logs_dir / "audit.jsonl"
        assert audit_file.is_file()
        assert "profile.delete" in audit_file.read_text(encoding="utf-8")
    finally:
        await db.close()


async def test_ensure_default_creates_primary(settings: Settings) -> None:
    settings.ensure_dirs()
    db = Database(settings.database_path)
    await db.init()
    manager = ProfileManager(
        settings, db, EventBus(), AuditLogger(db, settings.logs_dir)
    )
    try:
        info = await manager.ensure_default()
        assert info is not None and info.id == settings.default_profile
        assert await manager.ensure_default() is None
    finally:
        await db.close()


def test_state_roundtrip(settings: Settings) -> None:
    write_state(settings, AppState(active_profile="alpha"))
    assert read_state(settings).active_profile == "alpha"
    assert selected_profile(settings, "fallback") == "alpha"
    state_file = settings.state_path
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_profile_layout_rejects_symlink(settings: Settings) -> None:
    from forgemesh_browser.profiles.storage import ensure_profile_layout

    root = settings.profiles_dir
    root.mkdir(parents=True, exist_ok=True)
    target = root / "linked"
    target.symlink_to(root / "elsewhere")
    with pytest.raises(SecurityError):
        ensure_profile_layout(settings, "linked")
