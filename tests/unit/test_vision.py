"""Image inspection, comparison, template location and OCR dependency errors."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from forgemesh_browser.core.exceptions import AIDependencyError, MediaError
from forgemesh_browser.media.image_manager import (
    compare,
    create_thumbnail,
    image_info,
    thumbnail_data_url,
)
from forgemesh_browser.vision.image_locator import locate
from forgemesh_browser.vision.ocr import ocr_available, ocr_image


@pytest.fixture
def screenshot(tmp_path: Path) -> Path:
    image = Image.new("L", (240, 160), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 30, 63, 53), fill=20)
    draw.rectangle((44, 34, 59, 49), fill=210)
    path = tmp_path / "screen.png"
    image.save(path)
    return path


def test_image_info_and_thumbnail(screenshot: Path, tmp_path: Path) -> None:
    info = image_info(screenshot)
    assert (info["width"], info["height"]) == (240, 160)
    assert info["format"] == "PNG"
    assert len(info["sha256"]) == 64

    data_url = thumbnail_data_url(screenshot, max_width=120)
    assert data_url.startswith("data:image/png;base64,")

    thumb = create_thumbnail(screenshot, tmp_path / "thumb.png", max_width=100)
    with Image.open(thumb) as image:
        assert image.width == 100
        assert image.height == 66

    with pytest.raises(MediaError):
        image_info(tmp_path / "missing.png")


def test_compare_images(screenshot: Path, tmp_path: Path) -> None:
    identical = compare(screenshot, screenshot)
    assert identical["identical"] is True
    assert identical["diff_ratio"] == 0

    changed = tmp_path / "changed.png"
    with Image.open(screenshot) as image:
        copy = image.convert("RGB")
        ImageDraw.Draw(copy).rectangle((200, 130, 215, 145), fill=(255, 0, 0))
        copy.save(changed)
    diff = compare(screenshot, changed, output=tmp_path / "diff.png")
    assert diff["identical"] is False
    assert diff["bbox"] is not None
    assert diff["diff_ratio"] > 0
    assert Path(diff["diff_image"]).is_file()

    smaller = tmp_path / "small.png"
    Image.new("RGB", (10, 10)).save(smaller)
    assert compare(screenshot, smaller)["same_size"] is False


def test_locate_exact_template(screenshot: Path, tmp_path: Path) -> None:
    template = tmp_path / "reference.png"
    with Image.open(screenshot) as image:
        image.crop((40, 30, 64, 54)).save(template)

    result = locate(screenshot, template, min_confidence=0.9)
    assert result["found"] is True
    top = result["matches"][0]
    assert (top["x"], top["y"]) == (40, 30)
    assert top["confidence"] > 0.99

    annotated = tmp_path / "annotated.png"
    locate(screenshot, template, min_confidence=0.9, output=annotated)
    assert annotated.is_file()


def test_locate_missing_template(screenshot: Path, tmp_path: Path) -> None:
    flat = tmp_path / "flat.png"
    Image.new("L", (16, 16), 255).save(flat)
    result = locate(screenshot, flat, min_confidence=0.9)
    assert result["found"] is False

    with pytest.raises(MediaError):
        locate(screenshot, tmp_path / "missing.png")


def test_ocr_requires_image_and_reports_missing_binary(screenshot: Path, tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("text", encoding="utf-8")
    with pytest.raises(MediaError):
        ocr_image(note)
    with pytest.raises(MediaError):
        ocr_image(tmp_path / "missing.png")

    if not ocr_available():
        with pytest.raises(AIDependencyError) as info:
            ocr_image(screenshot)
        assert "tesseract-ocr" in str(info.value)
    else:  # pragma: no cover - depends on the host installation
        result = ocr_image(screenshot)
        assert result["characters"] >= 0
