"""Screenshots, uploads, downloads, assets, OCR and image location."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from forgemesh_browser.api.deps import get_runtime
from forgemesh_browser.core.exceptions import MediaError
from forgemesh_browser.core.runtime import Runtime
from forgemesh_browser.media.metadata import is_image
from forgemesh_browser.security.path_validation import (
    ensure_output_path,
    is_within,
    sanitize_filename,
    validate_user_file,
)
from forgemesh_browser.vision import image_locator
from forgemesh_browser.vision.image_comparison import compare_images
from forgemesh_browser.vision.ocr import ocr_image

router = APIRouter(tags=["media"])


class ScreenshotRequest(BaseModel):
    profile: str | None = None
    full_page: bool = False
    selector: str | None = None
    tab_id: str | None = None


class DownloadWait(BaseModel):
    profile: str | None = None
    timeout: float = Field(default=60.0, ge=1, le=600)


class LocateRequest(BaseModel):
    reference: str
    screenshot: str | None = None
    profile: str | None = None
    tab_id: str | None = None
    min_confidence: float = Field(default=0.75, ge=0.1, le=1.0)
    max_results: int = Field(default=5, ge=1, le=20)


class OcrRequest(BaseModel):
    path: str
    language: str | None = None


class CompareRequest(BaseModel):
    first: str
    second: str
    output: str | None = None


@router.post("/screenshots")
async def screenshot(
    payload: ScreenshotRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    profile = payload.profile or runtime.profiles.current()
    return await runtime.sessions.screenshot(
        profile,
        full_page=payload.full_page,
        selector=payload.selector,
        tab_id=payload.tab_id,
    )


@router.post("/media/download/wait")
async def download_wait(
    payload: DownloadWait, runtime: Runtime = Depends(get_runtime)
) -> dict:
    profile = payload.profile or runtime.profiles.current()
    return await runtime.browser.wait_for_download(profile, timeout=payload.timeout)


@router.get("/media")
async def list_media(
    profile: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    assets = await runtime.asset_store.list(
        profile_id=profile, kind=kind, limit=max(1, min(limit, 500))
    )
    return {"assets": assets}


@router.post("/media/upload")
async def upload(
    file: UploadFile = File(...),
    profile: str | None = Form(None),
    selector: str | None = Form(None),
    tab_id: str | None = Form(None),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    profile_id = profile or runtime.profiles.current()
    await runtime.profiles.require(profile_id)
    runtime.settings.ensure_dirs()
    temp = runtime.settings.runtime_dir / f"upload-{uuid.uuid4().hex[:8]}-{sanitize_filename(file.filename or 'file')}"
    limit = runtime.settings.upload_max_bytes
    total = 0
    with temp.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                handle.close()
                temp.unlink(missing_ok=True)
                raise MediaError(f"Upload exceeds limit ({limit} bytes)")
            handle.write(chunk)
    temp.chmod(0o600)
    try:
        path, meta = await asyncio.to_thread(runtime.uploads.prepare, temp)
        result = await runtime.browser.upload(
            profile_id, path, selector=selector, tab_id=tab_id
        )
        return {**result, "metadata": meta.to_dict()}
    finally:
        temp.unlink(missing_ok=True)


@router.get("/media/{asset_id}")
async def get_media(asset_id: str, runtime: Runtime = Depends(get_runtime)):
    try:
        asset = await runtime.asset_store.get(asset_id)
    except KeyError:
        raise MediaError(f"Asset not found: {asset_id}") from None
    path = Path(asset["path"])
    if not path.is_file() or not is_within(path, runtime.settings.data_dir):
        raise MediaError("Asset file is missing or outside the data directory")
    return FileResponse(
        path=path, media_type=asset["mime_type"], filename=asset["original_name"]
    )


@router.post("/locate")
async def locate(payload: LocateRequest, runtime: Runtime = Depends(get_runtime)) -> dict:
    screenshot_path = Path(payload.screenshot) if payload.screenshot else None
    if screenshot_path is None:
        profile = payload.profile or runtime.profiles.current()
        shot = await runtime.browser.screenshot(profile, tab_id=payload.tab_id)
        screenshot_path = Path(shot["path"])
    output = runtime.settings.results_dir / f"locate-{uuid.uuid4().hex[:8]}.png"
    return image_locator.locate(
        screenshot_path,
        payload.reference,
        min_confidence=payload.min_confidence,
        max_results=payload.max_results,
        output=output,
    )


@router.post("/ocr")
async def ocr(payload: OcrRequest, runtime: Runtime = Depends(get_runtime)) -> dict:
    return ocr_image(
        payload.path, lang=payload.language or runtime.settings.ocr_lang
    )


def _validated_image(path: str) -> Path:
    target = validate_user_file(path)
    if not is_image(target):
        raise MediaError(f"Not a supported image: {target}")
    return target


@router.post("/compare")
async def compare(payload: CompareRequest, runtime: Runtime = Depends(get_runtime)) -> dict:
    first = _validated_image(payload.first)
    second = _validated_image(payload.second)
    output = (
        ensure_output_path(payload.output, base=runtime.settings.results_dir)
        if payload.output
        else None
    )
    return compare_images(first, second, output=str(output) if output else None)
