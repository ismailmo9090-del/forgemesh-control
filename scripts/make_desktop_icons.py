"""Generate the desktop icon and tray icon for ForgeMesh Control Center.

The mark matches the dashboard: four cream squares on a dark rounded tile.
Run with the project venv: ``python scripts/make_desktop_icons.py``.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "desktop" / "assets"
CREAM = (231, 212, 166, 255)
CREAM_DIM = (168, 154, 120, 255)
DARK = (11, 16, 22, 255)
DARK_EDGE = (27, 35, 44, 255)


def draw_mark(size: int, *, background: bool) -> Image.Image:
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if background:
        radius = int(canvas * 0.22)
        draw.rounded_rectangle(
            (0, 0, canvas - 1, canvas - 1), radius=radius, fill=DARK, outline=DARK_EDGE, width=max(2, canvas // 128)
        )
        draw.rounded_rectangle(
            (
                int(canvas * 0.055),
                int(canvas * 0.055),
                int(canvas * 0.945),
                int(canvas * 0.945),
            ),
            radius=int(canvas * 0.17),
            outline=(45, 55, 66, 255),
            width=max(1, canvas // 160),
        )

    margin = canvas * (0.27 if background else 0.06)
    gap = canvas * (0.035 if background else 0.07)
    inner = canvas - margin * 2
    cell = (inner - gap) / 2
    radius = max(2, int(cell * 0.2))
    for row in range(2):
        for column in range(2):
            x = margin + column * (cell + gap)
            y = margin + row * (cell + gap)
            filled = (row, column) in {(0, 1), (1, 0)}
            draw.rounded_rectangle(
                (x, y, x + cell, y + cell),
                radius=radius,
                fill=CREAM if filled else None,
                outline=CREAM if filled else CREAM_DIM,
                width=max(2, int(cell * 0.075)),
            )

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    draw_mark(512, background=True).save(ASSETS / "icon.png")
    draw_mark(128, background=True).save(ASSETS / "icon-128.png")
    draw_mark(32, background=False).save(ASSETS / "tray.png")
    print(f"wrote {ASSETS / 'icon.png'}, icon-128.png, tray.png")


if __name__ == "__main__":
    main()
