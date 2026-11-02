"""Template matching using normalized cross-correlation (NumPy FFT).

Returns ranked bounding boxes with confidence scores. The screenshot/reference
is never mutated; an optional annotated copy can be written for debugging.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from forgemesh_browser.core.exceptions import MediaError
from forgemesh_browser.media.metadata import is_image

# Minimum per-pixel variance (grey levels²) for a window to carry signal.
_MIN_WINDOW_VARIANCE = 4.0


def _load_gray(path: Path) -> np.ndarray:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise MediaError("Pillow is required for image matching") from exc
    if not path.is_file() or not is_image(path):
        raise MediaError(f"Not a supported image: {path}")
    with Image.open(path) as image:
        return np.asarray(image.convert("L"), dtype=np.float32)


def _window_sums(values: np.ndarray, th: int, tw: int) -> np.ndarray:
    integral = np.pad(values.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    return (
        integral[th:, tw:]
        - integral[:-th, tw:]
        - integral[th:, :-tw]
        + integral[:-th, :-tw]
    )


def _ncc(image: np.ndarray, template: np.ndarray) -> np.ndarray | None:
    ih, iw = image.shape
    th, tw = template.shape
    if th > ih or tw > iw or th < 8 or tw < 8:
        return None
    centered = template - float(template.mean())
    template_energy = float((centered * centered).sum())
    if template_energy < 1e-6:
        return None

    fh, fw = ih + th - 1, iw + tw - 1
    image_fft = np.fft.rfft2(image, s=(fh, fw))
    template_fft = np.fft.rfft2(centered[::-1, ::-1], s=(fh, fw))
    correlation = np.fft.irfft2(image_fft * template_fft, s=(fh, fw))
    correlation = correlation[th - 1 : ih, tw - 1 : iw]

    sums = _window_sums(image, th, tw)
    sums_sq = _window_sums(image * image, th, tw)
    pixels = th * tw
    variance = np.maximum(sums_sq - (sums * sums) / pixels, 0.0)
    # Flat (near-uniform) windows have no usable signal; their ratio is noise.
    valid = (variance / pixels) >= _MIN_WINDOW_VARIANCE
    denominator = np.sqrt(template_energy * variance) + 1e-6
    scores = np.where(valid, correlation / denominator, 0.0)
    return np.clip(scores, -1.0, 1.0)


def _peaks(
    scores: np.ndarray,
    *,
    scale: float,
    template_size: tuple[int, int],
    min_confidence: float,
    max_results: int,
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    th, tw = template_size
    flat = scores.ravel()
    if flat.size == 0:
        return []
    count = min(flat.size, 2000)
    top = np.argpartition(flat, -count)[-count:]
    top = top[np.argsort(flat[top])[::-1]]
    picked: list[dict[str, Any]] = list(existing)
    for index in top:
        confidence = float(flat[index])
        if confidence < min_confidence:
            break
        y, x = divmod(int(index), scores.shape[1])
        candidate = {
            "x": int(x),
            "y": int(y),
            "width": int(tw),
            "height": int(th),
            "confidence": round(confidence, 4),
            "scale": round(scale, 3),
        }
        if all(
            abs(candidate["x"] - other["x"]) > tw * 0.5
            or abs(candidate["y"] - other["y"]) > th * 0.5
            for other in picked
        ):
            picked.append(candidate)
            if len(picked) >= max_results:
                break
    return _rank_matches(picked, max_results)


def _rank_matches(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: item["confidence"], reverse=True)[:limit]


def locate(
    screenshot: str | Path,
    reference: str | Path,
    *,
    min_confidence: float = 0.75,
    max_results: int = 5,
    output: str | Path | None = None,
) -> dict[str, Any]:
    haystack_path = Path(screenshot)
    needle_path = Path(reference)
    haystack = _load_gray(haystack_path)
    needle = _load_gray(needle_path)

    ih, iw = haystack.shape
    nh, nw = needle.shape
    height, width = ih, iw
    scales = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.45, 0.4]
    matches: list[dict[str, Any]] = []
    for scale in scales:
        th, tw = int(round(nh * scale)), int(round(nw * scale))
        if th > ih or tw > iw or th < 8 or tw < 8:
            continue
        from PIL import Image

        with Image.open(needle_path) as image:
            resized = image.convert("L").resize((tw, th))
        template = np.asarray(resized, dtype=np.float32)
        scores = _ncc(haystack, template)
        if scores is None:
            continue
        matches = _peaks(
            scores,
            scale=scale,
            template_size=(th, tw),
            min_confidence=min_confidence,
            max_results=max_results,
            existing=matches,
        )
        if len(matches) >= max_results and scale <= 0.7:
            break

    result: dict[str, Any] = {
        "screenshot": str(haystack_path),
        "reference": str(needle_path),
        "image_size": {"width": width, "height": height},
        "reference_size": {"width": nw, "height": nh},
        "matches": _rank_matches(matches, max_results),
        "found": bool(matches),
    }

    if output and matches:
        from PIL import Image, ImageDraw

        with Image.open(haystack_path).convert("RGB") as image:
            draw = ImageDraw.Draw(image)
            for match in matches:
                box = (
                    match["x"],
                    match["y"],
                    match["x"] + match["width"],
                    match["y"] + match["height"],
                )
                draw.rectangle(box, outline=(255, 64, 64), width=3)
                draw.text(
                    (match["x"] + 4, max(0, match["y"] - 14)),
                    f"{match['confidence']:.2f}",
                    fill=(255, 64, 64),
                )
            target = Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target)
            target.chmod(0o600)
        result["annotated"] = str(target)
    return result
