"""Filesystem permission helpers for sensitive runtime data."""

from __future__ import annotations

import os
from pathlib import Path

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, _DIR_MODE)
    except OSError:  # pragma: no cover - non-POSIX or restricted mount
        pass
    return path


def secure_file(path: Path) -> Path:
    if path.exists():
        try:
            os.chmod(path, _FILE_MODE)
        except OSError:  # pragma: no cover
            pass
    return path


def secure_tree(path: Path) -> None:
    """Tighten permissions on a directory tree without following symlinks."""

    if not path.exists():
        return
    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        try:
            os.chmod(root_path, _DIR_MODE)
        except OSError:
            pass
        for name in files:
            target = root_path / name
            if target.is_symlink():
                continue
            try:
                os.chmod(target, _FILE_MODE)
            except OSError:
                pass
        for name in dirs:
            target = root_path / name
            if target.is_symlink():
                dirs.remove(name)
