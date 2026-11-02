"""Console and rotating file logging with optional JSON lines."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from forgemesh_browser.security.permissions import secure_file
from forgemesh_browser.settings import Settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.__dict__.get("event"):
            payload["event"] = record.__dict__["event"]
        return json.dumps(payload, default=str)


def configure_logging(
    settings: Settings,
    *,
    verbose: bool = False,
    quiet: bool = False,
    json_mode: bool = False,
) -> None:
    level_name = "DEBUG" if verbose else ("ERROR" if quiet else settings.log_level.upper())
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if not quiet:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
        )
        root.addHandler(console)

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    path = settings.logs_dir / "forgemesh.log"
    file_handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter() if json_mode else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    root.addHandler(file_handler)
    secure_file(path)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def tail_file(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])
