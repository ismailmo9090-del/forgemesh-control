"""Image inspection, thumbnails and preview data URLs."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from forgemesh_browser.core.exceptions import MediaError
from forgemesh_browser.media.metadata import file_metadata, is_image


def image_info(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not is_image(target):
        raise MediaError(f"Not a supported image: {target}")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise MediaError("Pillow is required for image inspection") from exc
    meta = file_metadata(target)
    with Image.open(target) as image:
        width, height = image.size
        mode = image.mode
        frames = getattr(image, "n_frames", 1)
        image_format = image.format
    return {
        **meta.to_dict(),
        "width": width,
        "height": height,
        "mode": mode,
        "frames": frames,
        "format": image_format,
    }


def thumbnail_data_url(path: str | Path, *, max_width: int = 640) -> str:
    target = Path(path)
    if not is_image(target):
        raise MediaError(f"Not a supported image: {target}")
    from PIL import Image

    with Image.open(target) as image:
        image = image.convert("RGBA") if image.mode in {"P", "LA"} else image.copy()
        width, height = image.size
        if width > max_width:
            height = int(height * max_width / width)
            image = image.resize((max_width, height))
        buffer = io.BytesIO()
        fmt = "PNG" if target.suffix.lower() == ".png" else "JPEG"
        if fmt == "JPEG":
            image = image.convert("RGB")
        image.save(buffer, format=fmt)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    mime = "image/png" if fmt == "PNG" else "image/jpeg"
    return f"data:{mime};base64,{encoded}"


def create_thumbnail(path: str | Path, output: str | Path, *, max_width: int = 640) -> Path:
    data_url = thumbnail_data_url(path, max_width=max_width)
    _, encoded = data_url.split(",", 1)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(encoded))
    target.chmod(0o600)
    return target


def compare(path_a: str | Path, path_b: str | Path, *, output: str | Path | None = None) -> dict[str, Any]:
    """Pixel difference between two same-size images."""

    from PIL import Image, ImageChops

    first = Image.open(path_a).convert("RGB")
    second = Image.open(path_b).convert("RGB")
    if first.size != second.size:
        return {
            "same_size": False,
            "size_a": first.size,
            "size_b": second.size,
            "identical": False,
        }
    diff = ImageChops.difference(first, second)
    bbox = diff.getbbox()
    histogram = diff.histogram()
    total = first.size[0] * first.size[1] * 3
    unchanged = sum(histogram[offset] for offset in (0, 256, 512))
    changed = total - unchanged
    result = {
        "same_size": True,
        "identical": bbox is None,
        "bbox": bbox,
        "diff_ratio": round(changed / total, 6) if total else 0.0,
    }
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        diff.save(target)
        target.chmod(0o600)
        result["diff_image"] = str(target)
    return result
