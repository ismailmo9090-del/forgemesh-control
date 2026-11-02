"""Document inspection helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forgemesh_browser.core.exceptions import MediaError
from forgemesh_browser.media.metadata import file_metadata, is_document


def document_info(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not is_document(target):
        raise MediaError(f"Not a supported document: {target}")
    meta = file_metadata(target)
    info: dict[str, Any] = meta.to_dict()
    if meta.suffix in {".txt", ".md", ".csv", ".json", ".xml", ".html", ".css", ".js"}:
        try:
            info["preview"] = target.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError as exc:
            raise MediaError(f"Could not read {target.name}: {exc}") from exc
    return info
