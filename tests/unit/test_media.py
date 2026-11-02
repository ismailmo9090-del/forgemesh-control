"""File metadata validation and asset manifest storage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import MediaError
from forgemesh_browser.media.asset_storage import AssetStore
from forgemesh_browser.media.metadata import file_metadata, validate_upload
from forgemesh_browser.security.path_validation import is_within
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database

HELLO_SHA = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_file_metadata(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello")
    meta = file_metadata(path)
    assert meta.filename == "note.txt"
    assert meta.size == 5
    assert meta.sha256 == HELLO_SHA
    assert meta.mime_type.startswith("text/")


def test_file_metadata_missing(tmp_path: Path) -> None:
    with pytest.raises(MediaError):
        file_metadata(tmp_path / "nope.txt")


def test_validate_upload_rules(tmp_path: Path) -> None:
    good = tmp_path / "doc.pdf"
    good.write_bytes(b"%PDF-1.4 test")
    assert validate_upload(good, max_bytes=1024).suffix == ".pdf"

    executable = tmp_path / "payload.exe"
    executable.write_bytes(b"MZ")
    with pytest.raises(MediaError):
        validate_upload(executable, max_bytes=1024)

    with pytest.raises(MediaError):
        validate_upload(good, max_bytes=2)

    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not an image")
    with pytest.raises(MediaError):
        validate_upload(corrupt, max_bytes=1024)

    image = tmp_path / "valid.png"
    Image.new("RGB", (4, 4), "red").save(image)
    assert validate_upload(image, max_bytes=10_000).mime_type == "image/png"


async def test_asset_store(tmp_path: Path, settings: Settings) -> None:
    db = Database(settings.database_path)
    await db.init()
    store = AssetStore(settings, db, EventBus())
    try:
        source = tmp_path / "report.txt"
        source.write_text("payload", encoding="utf-8")
        data = await store.store_path(
            source, kind="download", original_name="report.txt", profile_id="primary"
        )

        assert not source.exists(), "store_path moves the source by default"
        stored = Path(data["path"])
        assert stored.is_file()
        assert stored.read_bytes() == b"payload"
        assert data["sha256"] == hashlib.sha256(b"payload").hexdigest()
        assert is_within(stored, settings.data_dir)
        assert data["profile_id"] == "primary"

        manifest = settings.assets_dir / "manifest.jsonl"
        lines = manifest.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[-1])["id"] == data["id"]

        fetched = await store.get(data["id"])
        assert fetched["path"] == data["path"]

        listed = await store.list(kind="download")
        assert [item["id"] for item in listed] == [data["id"]]

        copy_source = tmp_path / "copy.txt"
        copy_source.write_text("keep me", encoding="utf-8")
        await store.store_path(copy_source, kind="upload", move=False)
        assert copy_source.exists()

        binary = await store.store_bytes(b"\x89PNG", "../../evil.png", kind="result")
        assert ".." not in Path(binary["path"]).name
        assert is_within(Path(binary["path"]), settings.data_dir)

        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        assert await store.record_reference(outside) is None

        with pytest.raises(KeyError):
            await store.get("missing")
    finally:
        await db.close()
