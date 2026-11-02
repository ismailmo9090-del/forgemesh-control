"""Verification that a profile's browser state is private and isolated."""

from __future__ import annotations

import os
import stat

from forgemesh_browser.profiles.models import IsolationReport
from forgemesh_browser.profiles.storage import profile_path
from forgemesh_browser.security.path_validation import is_within
from forgemesh_browser.settings import Settings

_BROWSER_MARKERS = (
    "Default",
    "Local Storage",
    "IndexedDB",
    "Cache",
    "Cookies",
    "Network",
    "Local State",
)


def inspect_isolation(settings: Settings, profile_id: str) -> IsolationReport:
    path = profile_path(settings, profile_id)
    root = settings.profiles_dir.resolve()
    exists = path.exists()
    mode = None
    private = False
    if exists:
        mode = oct(stat.S_IMODE(path.stat().st_mode))
        private = os.access(path, os.R_OK) and int(mode, 8) & 0o077 == 0
    browser_dirs = []
    if exists and path.is_dir():
        browser_dirs = sorted(
            entry.name for entry in path.iterdir() if entry.name in _BROWSER_MARKERS
        )

    shared: list[str] = []
    if path.exists():
        for other in root.iterdir():
            if other.name == profile_id or not other.is_dir():
                continue
            other_resolved = other.resolve()
            if other_resolved == path or is_within(path, other_resolved):
                shared.append(other.name)

    return IsolationReport(
        profile_id=profile_id,
        path=str(path),
        within_root=is_within(path, root),
        is_symlink=path.is_symlink(),
        mode=mode,
        private=private,
        browser_dirs=browser_dirs,
        shared_with_other_profiles=shared,
    )
