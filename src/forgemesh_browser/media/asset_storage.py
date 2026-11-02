"""Asset storage and manifest (SQLite rows + append-only JSONL)."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.media.metadata import FileMetadata, file_metadata
from forgemesh_browser.security.path_validation import is_within, sanitize_filename
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database
from forgemesh_browser.storage.models import AssetRow, as_utc
from forgemesh_browser.storage.repositories import assets as asset_repo


def _category(kind: str) -> str:
    return {
        "screenshot": "screenshots",
        "screen": "screenshots",
        "download": "downloads",
        "upload": "uploads",
        "image": "generated-images",
        "document": "documents",
        "result": "results",
    }.get(kind, sanitize_filename(kind, "files"))


def _row_to_dict(row: AssetRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "kind": row.kind,
        "original_name": row.original_name,
        "path": row.stored_path,
        "source_url": row.source_url,
        "provider_id": row.provider_id,
        "profile_id": row.profile_id,
        "mime_type": row.mime_type,
        "size": row.size,
        "sha256": row.sha256,
        "created_at": as_utc(row.created_at).isoformat() if row.created_at else None,
    }


class AssetStore:
    def __init__(self, settings: Settings, db: Database, events: EventBus) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.manifest_path = settings.assets_dir / "manifest.jsonl"

    def _target(
        self,
        project_id: str,
        provider_id: str | None,
        kind: str,
        filename: str,
        digest: str,
    ) -> Path:
        project = sanitize_filename(project_id, "default")
        provider = sanitize_filename(provider_id or "general", "general")
        safe_name = sanitize_filename(filename, "file")
        directory = self.settings.assets_dir / project / provider / _category(kind)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return directory / f"{stamp}-{digest[:8]}-{safe_name}"

    def _append_manifest(self, data: dict[str, Any]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, default=str) + "\n")
        self.manifest_path.chmod(0o600)

    async def store_path(
        self,
        source: Path,
        *,
        kind: str = "download",
        original_name: str | None = None,
        source_url: str | None = None,
        provider_id: str | None = None,
        profile_id: str | None = None,
        project_id: str = "default",
        move: bool = True,
        metadata: FileMetadata | None = None,
    ) -> dict[str, Any]:
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        meta = metadata or file_metadata(source)
        target = self._target(
            project_id, provider_id, kind, original_name or source.name, meta.sha256
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(source), target)
        else:
            shutil.copy2(source, target)
        target.chmod(0o600)

        row = AssetRow(
            id=uuid.uuid4().hex[:12],
            project_id=sanitize_filename(project_id, "default"),
            kind=kind,
            original_name=original_name or source.name,
            stored_path=str(target),
            source_url=source_url,
            provider_id=provider_id,
            profile_id=profile_id,
            mime_type=meta.mime_type,
            size=meta.size,
            sha256=meta.sha256,
        )
        async with self.db.session_scope() as session:
            await asset_repo.insert(session, row)
            data = _row_to_dict(row)
        self._append_manifest(data)
        self.events.publish("media.stored", **data)
        return data

    async def record_reference(
        self,
        path: Path,
        *,
        kind: str = "result",
        source_url: str | None = None,
        provider_id: str | None = None,
        profile_id: str | None = None,
        project_id: str = "default",
    ) -> dict[str, Any] | None:
        """Register a file that already lives inside the data directory."""

        path = path.resolve()
        if not path.is_file() or not is_within(path, self.settings.data_dir):
            return None
        meta = file_metadata(path)
        row = AssetRow(
            id=uuid.uuid4().hex[:12],
            project_id=sanitize_filename(project_id, "default"),
            kind=kind,
            original_name=path.name,
            stored_path=str(path),
            source_url=source_url,
            provider_id=provider_id,
            profile_id=profile_id,
            mime_type=meta.mime_type,
            size=meta.size,
            sha256=meta.sha256,
        )
        async with self.db.session_scope() as session:
            await asset_repo.insert(session, row)
            data = _row_to_dict(row)
        self._append_manifest(data)
        self.events.publish("media.stored", **data)
        return data

    async def store_bytes(
        self,
        data: bytes,
        filename: str,
        *,
        kind: str = "result",
        provider_id: str | None = None,
        profile_id: str | None = None,
        project_id: str = "default",
    ) -> dict[str, Any]:
        tmp_dir = self.settings.data_dir / "runtime"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = tmp_dir / f"upload-{uuid.uuid4().hex[:8]}-{sanitize_filename(filename)}"
        temp_path.write_bytes(data)
        try:
            return await self.store_path(
                temp_path,
                kind=kind,
                original_name=filename,
                provider_id=provider_id,
                profile_id=profile_id,
                project_id=project_id,
                move=True,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    async def get(self, asset_id: str) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await asset_repo.get(session, asset_id)
            if row is None:
                raise KeyError(asset_id)
            data = _row_to_dict(row)
        if not is_within(Path(data["path"]), self.settings.data_dir):
            raise PermissionError("Asset path escapes the data directory")
        return data

    async def list(
        self,
        *,
        profile_id: str | None = None,
        kind: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            rows = await asset_repo.list_all(
                session,
                profile_id=profile_id,
                kind=kind,
                project_id=project_id,
                limit=limit,
            )
            return [_row_to_dict(row) for row in rows]
