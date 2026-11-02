"""Secret handling.

Passwords are never accepted or stored by this application. Website
authentication happens through the browser profile, and API credentials are
read from environment variables. The only persisted secret is the random
localhost API token, stored with mode 0600.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from forgemesh_browser.security.permissions import secure_file


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def read_or_create_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            secure_file(path)
            return token
    token = generate_token()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    secure_file(path)
    return token


def load_secret_store(path: Path) -> dict[str, str]:
    """Load optional local secrets (never passwords) from a 0600 JSON file."""

    if not path.is_file():
        return {}
    secure_file(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def redact(value: str, *, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"
