"""Path and filename validation to contain file access to approved roots."""

from __future__ import annotations

import re
from pathlib import Path

from forgemesh_browser.core.exceptions import SecurityError

_UNSAFE = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"COM{i}" for i in range(1, 10)
} | {f"LPT{i}" for i in range(1, 10)}


def sanitize_filename(name: str, fallback: str = "file") -> str:
    name = Path(name.replace("\\", "/")).name.strip()
    name = _UNSAFE.sub("_", name)
    name = name.strip(". ")
    if not name or name in {".", ".."}:
        name = fallback
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name[:180]


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def safe_join(base: Path, *parts: str) -> Path:
    """Join untrusted path parts under ``base``; reject traversal and symlinks."""

    candidate = base.joinpath(*parts)
    target = candidate.resolve()
    if not is_within(target, base):
        raise SecurityError(f"Path escapes allowed directory: {candidate}")
    return target


def validate_user_file(path: str | Path, *, max_bytes: int | None = None) -> Path:
    """Validate a file explicitly provided by the local user for upload."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.is_symlink():
        raise SecurityError("Symlinked files are not accepted for upload")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SecurityError(f"File does not exist: {candidate}") from exc
    if not resolved.is_file():
        raise SecurityError(f"Not a regular file: {resolved}")
    if max_bytes is not None and resolved.stat().st_size > max_bytes:
        raise SecurityError(
            f"File exceeds upload limit ({resolved.stat().st_size} > {max_bytes} bytes)"
        )
    return resolved


def ensure_output_path(path: str | Path, *, base: Path) -> Path:
    """Resolve an output path. Relative paths are confined to ``base``."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        parent = (base / candidate.parent).resolve()
        if not is_within(parent, base):
            raise SecurityError(f"Output path escapes allowed directory: {candidate}")
    else:
        parent = candidate.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    return parent / sanitize_filename(candidate.name)
