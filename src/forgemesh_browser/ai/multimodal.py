"""Image loading for multimodal requests (bounded size, no mutation of source)."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from forgemesh_browser.core.exceptions import MediaError
from forgemesh_browser.media.metadata import is_image

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 2048


@dataclass(slots=True)
class ImagePayload:
    path: Path
    mime_type: str
    data_base64: str
    width: int = 0
    height: int = 0

    @property
    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{self.data_base64}"

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "bytes": len(base64.b64decode(self.data_base64)),
        }


def load_images(paths: list[str | Path], *, max_images: int = MAX_IMAGES) -> list[ImagePayload]:
    if len(paths) > max_images:
        raise MediaError(f"At most {max_images} images per request")
    payloads: list[ImagePayload] = []
    for item in paths:
        path = Path(item).expanduser().resolve()
        if not path.is_file() or not is_image(path):
            raise MediaError(f"Unsupported image: {path}")
        size = path.stat().st_size
        if size <= MAX_IMAGE_BYTES:
            raw = path.read_bytes()
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            if path.suffix.lower() == ".webp":
                mime = "image/webp"
            if path.suffix.lower() == ".gif":
                mime = "image/gif"
            payloads.append(
                ImagePayload(
                    path=path,
                    mime_type=mime,
                    data_base64=base64.b64encode(raw).decode("ascii"),
                )
            )
            continue

        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88)
            width, height = image.size
        payloads.append(
            ImagePayload(
                path=path,
                mime_type="image/jpeg",
                data_base64=base64.b64encode(buffer.getvalue()).decode("ascii"),
                width=width,
                height=height,
            )
        )
    return payloads
