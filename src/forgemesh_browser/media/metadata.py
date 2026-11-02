"""File metadata, checksums and MIME validation."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import asdict, dataclass
from pathlib import Path

from forgemesh_browser.core.exceptions import MediaError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".bmp", ".tiff"}
DOCUMENT_SUFFIXES = {".pdf", ".txt", ".md", ".csv", ".json", ".xml", ".html", ".css", ".js"}
ARCHIVE_SUFFIXES = {".zip", ".tar", ".gz", ".tgz", ".7z"}
UPLOAD_ALLOWED_SUFFIXES = IMAGE_SUFFIXES | DOCUMENT_SUFFIXES | ARCHIVE_SUFFIXES

for suffix in IMAGE_SUFFIXES | DOCUMENT_SUFFIXES | ARCHIVE_SUFFIXES:
    mimetypes.add_type(
        {
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
            ".md": "text/markdown",
            ".json": "application/json",
            ".zip": "application/zip",
        }.get(suffix, mimetypes.guess_type(f"x{suffix}")[0] or "application/octet-stream"),
        suffix,
    )


@dataclass(slots=True)
class FileMetadata:
    filename: str
    size: int
    mime_type: str
    sha256: str
    suffix: str

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def detect_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def file_metadata(path: Path) -> FileMetadata:
    if not path.is_file():
        raise MediaError(f"File not found: {path}")
    stat = path.stat()
    return FileMetadata(
        filename=path.name,
        size=stat.st_size,
        mime_type=detect_mime(path),
        sha256=sha256_file(path),
        suffix=path.suffix.lower(),
    )


def validate_upload(path: Path, *, max_bytes: int) -> FileMetadata:
    meta = file_metadata(path)
    if meta.size > max_bytes:
        raise MediaError(
            f"Upload exceeds limit ({meta.size} > {max_bytes} bytes)"
        )
    if meta.suffix not in UPLOAD_ALLOWED_SUFFIXES:
        raise MediaError(
            f"Unsupported upload type '{meta.suffix or '(none)'}'. Allowed: "
            + ", ".join(sorted(UPLOAD_ALLOWED_SUFFIXES))
        )
    if meta.suffix in IMAGE_SUFFIXES and meta.suffix != ".svg":
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            raise MediaError(f"Corrupt or invalid image file: {path.name}") from exc
    return meta


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def is_document(path: Path) -> bool:
    return path.suffix.lower() in DOCUMENT_SUFFIXES
