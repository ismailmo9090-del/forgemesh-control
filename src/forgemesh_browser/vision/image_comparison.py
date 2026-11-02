"""Visual comparison between two screenshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forgemesh_browser.media.image_manager import compare


def compare_images(
    first: str | Path, second: str | Path, *, output: str | Path | None = None
) -> dict[str, Any]:
    return compare(first, second, output=output)
