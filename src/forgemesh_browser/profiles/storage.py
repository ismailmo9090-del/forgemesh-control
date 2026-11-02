"""Disk layout for isolated browser profiles and the selected-profile state."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel

from forgemesh_browser.core.exceptions import ProfileError, SecurityError
from forgemesh_browser.security.permissions import ensure_private_dir, secure_file
from forgemesh_browser.settings import Settings

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AppState(BaseModel):
    active_profile: str | None = None


def validate_profile_id(profile_id: str) -> str:
    normalized = profile_id.strip().lower()
    if not PROFILE_ID_RE.match(normalized):
        raise ProfileError(
            "Profile id must be 1-64 characters: lowercase letters, digits, '-' or '_'"
        )
    if normalized in {"profiles", "runtime", ".", ".."}:
        raise ProfileError(f"Reserved profile id: {normalized}")
    return normalized


def profile_path(settings: Settings, profile_id: str) -> Path:
    validate_profile_id(profile_id)
    root = settings.profiles_dir.resolve()
    candidate = root / profile_id
    if candidate.is_symlink():
        raise SecurityError(f"Profile directory is a symlink: {candidate}")
    target = candidate.resolve()
    if root not in target.parents and target != root:
        raise SecurityError("Profile path escapes the profiles directory")
    return target


def ensure_profile_layout(settings: Settings, profile_id: str) -> Path:
    path = profile_path(settings, profile_id)
    if path.exists() and path.is_symlink():
        raise SecurityError(f"Profile directory is a symlink: {path}")
    ensure_private_dir(path)
    return path


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            target = Path(root) / name
            if target.is_symlink():
                continue
            try:
                total += target.stat().st_size
            except OSError:
                continue
    return total


def read_state(settings: Settings) -> AppState:
    path = settings.state_path
    if not path.is_file():
        return AppState()
    try:
        return AppState.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return AppState()


def write_state(settings: Settings, state: AppState) -> None:
    path = settings.state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.model_dump(), handle, indent=2)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    secure_file(path)


def selected_profile(settings: Settings, fallback: str) -> str:
    state = read_state(settings)
    return state.active_profile or fallback
