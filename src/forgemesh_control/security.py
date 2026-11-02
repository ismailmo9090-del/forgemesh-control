"""Tokens, path containment and worker authentication helpers."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path

from forgemesh_browser.security.secrets import read_or_create_token  # reuse, 0600

_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous characters
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


class SecurityError(RuntimeError):
    """Raised when a request would escape a security boundary."""


def control_token(path: Path) -> str:
    return read_or_create_token(path)


def new_worker_token() -> str:
    return secrets.token_urlsafe(40)


def new_pairing_code(length: int = 8) -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(length))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str | None, expected_hash: str) -> bool:
    if not token or not expected_hash:
        return False
    return hmac.compare_digest(hash_token(token), expected_hash)


def safe_slug(value: str, *, fallback: str = "item", max_length: int = 40) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip(".-_")
    slug = slug[:max_length].strip(".-_")
    return slug or fallback


def safe_filename(value: str, *, fallback: str = "upload") -> str:
    name = Path(value).name
    name = re.sub(r"[\x00-\x1f/\\]", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        return fallback
    return name[:240]


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and refuse anything outside ``root``."""
    root_resolved = root.expanduser().resolve()
    target = candidate.expanduser().resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise SecurityError(f"Path escapes the allowed root: {candidate}")
    return target


def redact_secret(value: str, *, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def is_loopback(host: str | None) -> bool:
    from forgemesh_control.config import _is_loopback

    return _is_loopback(host or "")
