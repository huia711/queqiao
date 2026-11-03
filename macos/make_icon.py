"""Render the 鹊桥 app icon (a red arch bridge on warm paper) to a .iconset with Pillow."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

PAPER = (246, 243, 238, 255)
PAPER_EDGE = (230, 223, 212, 255)
ACCENT = (139, 46, 60, 255)
ACCENT_SOFT = (139, 46, 60, 120)
WATER = (139, 46, 60, 70)


def render(size: int) -> Image.Image:
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = round(canvas * 0.09)
    radius = round(canvas * 0.2)
    draw.rounded_rectangle([inset, inset, canvas - inset, canvas - inset], radius=radius, fill=PAPER, outline=PAPER_EDGE, width=max(2, canvas // 160))

    unit = canvas / 32
    width = round(unit * 1.9)

    def point(x: float, y: float) -> tuple[float, float]:
        return (unit * x, unit * y)

    # Arch deck: quadratic curve sampled as a polyline.
    left, right, top, base = 5.2, 26.8, 10.6, 21.6
    samples = []
    for step in range(0, 101):
        t = step / 100
        x = left + (right - left) * t
        y = base - (base - top) * (1 - (2 * t - 1) ** 2)
        samples.append(point(x, y))
    draw.line(samples, fill=ACCENT, width=width, joint="curve")
    for x in (left, right):
        cx, cy = point(x, base)
        draw.ellipse([cx - width / 2, cy - width / 2, cx + width / 2, cy + width / 2], fill=ACCENT)

    # Hangers between the deck and the road.
    for x in (9.0, 12.6, 16.0, 19.4, 23.0):
        t = (x - left) / (right - left)
        y = base - (base - top) * (1 - (2 * t - 1) ** 2)
        draw.line([point(x, y + 1.4), point(x, base)], fill=ACCENT_SOFT, width=round(unit * 1.15))

    # Road and water lines.
    draw.line([point(4.4, base + 0.2), point(27.6, base + 0.2)], fill=ACCENT, width=round(unit * 1.2))
    draw.line([point(7.5, 24.6), point(24.5, 24.6)], fill=WATER, width=round(unit * 0.9))
    draw.line([point(10.5, 26.6), point(21.5, 26.6)], fill=WATER, width=round(unit * 0.8))

    # The magpie: a small dot above the arch.
    cx, cy = point(16, 6.6)
    r = unit * 1.75
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)

    return image.resize((size, size), Image.LANCZOS)


def main(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        render(base).save(target / f"icon_{base}x{base}.png")
        render(base * 2).save(target / f"icon_{base}x{base}@2x.png")
    render(1024).save(target.parent / "icon-1024.png")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
