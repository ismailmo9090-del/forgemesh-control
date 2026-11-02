"""OCR through the system Tesseract binary (optional dependency).

No Python OCR package is required. When Tesseract is missing the caller gets a
clear dependency error instead of an empty string.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from forgemesh_browser.core.exceptions import AIDependencyError, MediaError
from forgemesh_browser.media.metadata import is_image

INSTALL_HINT = (
    "Tesseract OCR is not installed. Install it with: "
    "sudo apt install tesseract-ocr (optionally tesseract-ocr-<lang>)"
)


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image(path: str | Path, *, lang: str = "eng", psm: int = 3) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise MediaError(f"Image not found: {target}")
    if not is_image(target):
        raise MediaError(f"OCR requires an image file: {target}")
    binary = shutil.which("tesseract")
    if binary is None:
        raise AIDependencyError(INSTALL_HINT)
    if psm < 0 or psm > 13:
        raise MediaError("Tesseract page segmentation mode must be 0-13")
    try:
        completed = subprocess.run(
            [binary, str(target), "stdout", "-l", lang, "--psm", str(psm)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError("OCR timed out after 120 seconds") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or f"tesseract exited {completed.returncode}"
        raise MediaError(f"OCR failed: {message}")
    text = completed.stdout.strip()
    return {
        "path": str(target),
        "text": text,
        "characters": len(text),
        "language": lang,
        "psm": psm,
    }
