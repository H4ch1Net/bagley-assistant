"""Render the app icons for the installable web app (``bagley/static/icons/``).

ctOS style, in the ctOS grays only: a ``#0E0E0E`` tile with a 1px ``#7A7A7A`` outline and
``#D9D9D9`` corner brackets around Bagley's node graph, the diamond hub (an outlined diamond with a
filled inner diamond and a vertical centre line) and two square satellite nodes.

Drawn on a 64-unit grid like the ctOS app icons, supersampled, then scaled down. The maskable icon
keeps everything inside the 80% safe zone (a circle of radius 40%) that launchers never crop.
Needs Pillow, which Bagley itself does not use:

    python3 scripts/icons.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "bagley" / "static" / "icons"

GROUND = "#0E0E0E"
SECONDARY = "#7A7A7A"
CHROME = "#D9D9D9"
SUPERSAMPLE = 4

HUB = (26.0, 36.0)
HUB_RADIUS = 12.0  # Half the diagonal of the outer diamond.
HUB_STROKE = 2.5
CORE_RADIUS = 4.5
CENTRE_LINE = 1.2
SATELLITES = [(46.0, 18.0), (47.5, 45.0)]
SATELLITE_SIZE = 6.0


def render(size: int, scale: float = 1.0) -> Image.Image:
    """The icon at ``size`` pixels, with the design shrunk by ``scale`` around the centre."""
    big = size * SUPERSAMPLE
    unit = big / 64

    def pt(x: float, y: float) -> tuple[float, float]:
        return ((32 + (x - 32) * scale) * unit, (32 + (y - 32) * scale) * unit)

    def rect(x0: float, y0: float, x1: float, y1: float, fill: str) -> None:
        (a, b), (c, d) = pt(min(x0, x1), min(y0, y1)), pt(max(x0, x1), max(y0, y1))
        draw.rectangle((a, b, c - 1, d - 1), fill=fill)

    def diamond(cx: float, cy: float, r: float, fill: str) -> None:
        draw.polygon([pt(cx, cy - r), pt(cx + r, cy), pt(cx, cy + r), pt(cx - r, cy)], fill=fill)

    def line(x0: float, y0: float, x1: float, y1: float, width: float, fill: str) -> None:
        length = math.hypot(x1 - x0, y1 - y0)
        nx, ny = -(y1 - y0) / length * width / 2, (x1 - x0) / length * width / 2
        corners = [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
        draw.polygon([pt(x, y) for x, y in corners], fill=fill)

    image = Image.new("RGB", (big, big), GROUND)
    draw = ImageDraw.Draw(image)

    # The tile: a 1px outline and the corner brackets (12-unit arms, 2 units thick).
    for x0, y0, x1, y1 in [(2.5, 2.5, 61.5, 3.5), (2.5, 60.5, 61.5, 61.5),
                           (2.5, 2.5, 3.5, 61.5), (60.5, 2.5, 61.5, 61.5)]:  # fmt: skip
        rect(x0, y0, x1, y1, SECONDARY)
    for sx, sy in [(1, 1), (-1, 1), (1, -1), (-1, -1)]:
        cx, cy = (2 if sx > 0 else 62), (2 if sy > 0 else 62)
        rect(cx, cy, cx + 13 * sx, cy + 2 * sy, CHROME)
        rect(cx, cy, cx + 2 * sx, cy + 13 * sy, CHROME)

    # Spine edges from the hub to each satellite, under the nodes.
    for x, y in SATELLITES:
        line(*HUB, x, y, 1.0, SECONDARY)

    # The hub: outlined diamond, vertical centre line, filled core.
    hx, hy = HUB
    diamond(hx, hy, HUB_RADIUS, CHROME)
    diamond(hx, hy, HUB_RADIUS - HUB_STROKE * math.sqrt(2), GROUND)
    reach = HUB_RADIUS - CENTRE_LINE / 2  # Ends flush with the outline's tips.
    line(hx, hy - reach, hx, hy + reach, CENTRE_LINE, CHROME)
    diamond(hx, hy, CORE_RADIUS, CHROME)

    # Satellite nodes.
    half = SATELLITE_SIZE / 2
    for x, y in SATELLITES:
        rect(x - half, y - half, x + half, y + half, CHROME)

    return image.resize((size, size), Image.Resampling.LANCZOS)


ICONS = {
    "icon-192.png": (192, 1.0),
    "icon-512.png": (512, 1.0),
    # Frame corners at 40% of the size from the centre: inside every launcher's mask.
    "maskable-512.png": (512, 0.6),
    # iOS rounds the corners by about 22%; pull the brackets in so none is clipped.
    "apple-touch-icon.png": (180, 0.9),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (size, scale) in ICONS.items():
        render(size, scale).save(OUT / name, optimize=True)
        print(f"{OUT / name}  {size}x{size}")


if __name__ == "__main__":
    main()
