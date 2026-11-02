"""Upload validation and preparation."""

from __future__ import annotations

from pathlib import Path

from forgemesh_browser.media.metadata import FileMetadata, validate_upload
from forgemesh_browser.security.path_validation import validate_user_file
from forgemesh_browser.settings import Settings


class UploadManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare(self, path: str | Path) -> tuple[Path, FileMetadata]:
        resolved = validate_user_file(path, max_bytes=self.settings.upload_max_bytes)
        metadata = validate_upload(resolved, max_bytes=self.settings.upload_max_bytes)
        return resolved, metadata
