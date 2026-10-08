"""Generate bagley/static/icons.svg, the UI's line glyphs in the ctOS style.

ctOS icons (design/ctos/assets/app-icons) are straight strokes with miter joins, secondary
strokes thinner, and the odd solid pixel. These are the same idea on a 16 by 16 grid: drawn for
square caps and miter joins, curves only where a shape needs one (a wifi arc, a dial). The
stroke width comes from the stylesheet (``.icon``); ``thin`` strokes are drawn lighter.

    python scripts/glyphs.py           # writes bagley/static/icons.svg
    python scripts/glyphs.py --sheet x.html  # also writes a contact sheet to look at
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bagley" / "static" / "icons.svg"


def p(d: str) -> str:
    return f'<path d="{d}"/>'


def thin(d: str) -> str:
    return f'<path d="{d}" stroke-width="1" opacity=".6"/>'


def bevel(d: str) -> str:
    """For outlines with cusps, where a miter would spike."""
    return f'<path d="{d}" stroke-linejoin="bevel"/>'


def px(x: float, y: float, w: float = 1.5, h: float | None = None) -> str:
    """A solid pixel block."""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h if h is not None else w}" fill="currentColor" stroke="none"/>'


def ring(cx: float, cy: float, r: float, *, faint: bool = False) -> str:
    extra = ' stroke-width="1" opacity=".6"' if faint else ""
    return f'<circle cx="{cx}" cy="{cy}" r="{r}"{extra}/>'


FILE = "M3 1.5h6.5L13 5v9.5H3z M9.5 1.5V5H13"
FOLDER = "M1.5 3h4.5l1.5 2h7v8.5h-13z"
SHIELD = "M8 1.5l6 2V9l-6 5.5L2 9V3.5z"
SPEAKER = "M1.5 5.5h3l4-3.5v12l-4-3.5h-3z"
MONITOR = "M1.5 2.5h13v8.5h-13z M5.5 14h5 M8 11v3"
BOX = "M2 2h12v12H2z"

GLYPHS: dict[str, list[str]] = {
    # Actions
    "plus": [p("M8 2.5v11M2.5 8h11")],
    "x": [p("M3.5 3.5l9 9M12.5 3.5l-9 9")],
    "check": [p("M2.5 8.5l3.5 3.5 7.5-7.5")],
    "search": [p("M2 2h8.5v8.5H2z"), p("M10.5 10.5l4 4")],
    "settings": [
        p("M1.5 4.5h13M1.5 8h13M1.5 11.5h13"),
        px(9, 3, 3),
        px(3.5, 6.5, 3),
        px(9.5, 10, 3),
    ],
    "copy": [p("M5.5 5.5h8.5v8.5H5.5z"), p("M10.5 5.5v-3.5H2v8.5h3.5")],
    "pencil": [p("M10.5 2l3.5 3.5-8.5 8.5H2v-3.5z"), thin("M8.5 4l3.5 3.5")],
    "trash-2": [
        p("M1.5 4h13"),
        p("M5.5 4V1.5h5V4"),
        p("M3.5 4l1 10.5h7l1-10.5"),
        thin("M6.5 7v4.5M9.5 7v4.5"),
    ],
    "eraser": [p("M9.5 1.5l5 5-7.5 7.5H4L1.5 11.5z"), thin("M5.5 5.5l5 5"), p("M8 14h6.5")],
    "refresh-cw": [
        p("M2.5 8V3.5h9"),
        p("M9.5 1.5l2 2-2 2"),
        p("M13.5 8v4.5h-9"),
        p("M6.5 14.5l-2-2 2-2"),
    ],
    "undo-2": [p("M5.5 2.5L2 6l3.5 3.5"), p("M2 6h10v7H6.5")],
    "download": [p("M8 1.5V10M4.5 6.5L8 10l3.5-3.5"), p("M2 10.5v3.5h12v-3.5")],
    "upload": [p("M8 10.5V2M4.5 5.5L8 2l3.5 3.5"), p("M2 10.5v3.5h12v-3.5")],
    "external-link": [p("M9.5 2h4.5v4.5"), p("M14 2L7.5 8.5"), p("M11.5 10v4H2V4.5h4")],
    "arrow-up": [p("M8 14V2.5M3.5 7L8 2.5 12.5 7")],
    "arrow-down": [p("M8 2v11.5M3.5 9L8 13.5 12.5 9")],
    "chevron-down": [p("M4 6l4 4 4-4")],
    "chevron-right": [p("M6 4l4 4-4 4")],
    "menu": [p("M2 4h12M2 8h12M2 12h12")],
    "panel-right": [p("M1.5 2.5h13v11h-13z"), p("M10 2.5v11")],
    "panel-left": [p("M1.5 2.5h13v11h-13z"), p("M6 2.5v11")],
    "square": [px(4, 4, 8)],
    "play": [p("M4 2.5l9 5.5-9 5.5z")],
    "power": [p("M8 1.5v6"), p("M5 3.5L2.5 6v4.5L5 14h6l2.5-3.5V6L11 3.5")],
    "paperclip": [p("M11 5v8H5V2.5h4.5V11h-2V5")],
    "scan": [p("M1.5 5V1.5H5M11 1.5h3.5V5M14.5 11v3.5H11M5 14.5H1.5V11"), p("M4.5 8h7")],
    # State
    "info": [p(BOX), p("M8 7v4.5"), px(7.25, 4, 1.5)],
    "circle-check": [p(BOX), p("M5 8l2 2 4-4")],
    "circle-x": [p(BOX), p("M5.5 5.5l5 5M10.5 5.5l-5 5")],
    "triangle-alert": [p("M8 1.5L15 14H1z"), p("M8 6v4"), px(7.25, 11.25, 1.5)],
    "loader-circle": [
        px(2, 2, 3),
        px(6.5, 2, 3),
        px(11, 2, 3),
        px(11, 6.5, 3),
        thin("M2.5 7h2v2h-2zM2.5 11.5h2v2h-2zM7 11.5h2v2H7zM11.5 11.5h2v2h-2z"),
    ],
    "lock": [p("M3 7h10v7.5H3z"), p("M5 7V3.5L6.5 2h3L11 3.5V7"), px(7.25, 9.5, 1.5, 2.5)],
    "key-round": [p("M1.5 5.5h5v5h-5z"), p("M6.5 8h8M12 8v3M14.5 8v2")],
    "eye": [p("M1 8l3.5-4.5h7L15 8l-3.5 4.5h-7z"), px(6.5, 6.5, 3)],
    "shield-check": [p(SHIELD), p("M5 7.5l2 2 4-4")],
    "shield-alert": [p(SHIELD), p("M8 4.5v4"), px(7.25, 10, 1.5)],
    "zap": [p("M9 1.5L3 9h4.5L7 14.5 13 7H8.5z")],
    "sparkles": [p("M8 1.5L14.5 8 8 14.5 1.5 8z"), thin("M8 1.5V8L5 11M8 8l3 3"), px(7, 9.5, 2, 2)],
    # Talk and sound
    "message-square": [p("M1.5 2h13v9.5H6.5L3.5 14.5v-3h-2z")],
    "mic": [p("M6 1.5h4v8H6z"), p("M3.5 7v2.5l2 2h5l2-2V7"), p("M8 11.5v3M5.5 14.5h5")],
    "mic-off": [
        p("M6 1.5h4v8H6z"),
        p("M3.5 7v2.5l2 2h5l2-2V7"),
        p("M8 11.5v3M5.5 14.5h5"),
        p("M1.5 1.5l13 13"),
    ],
    "volume-2": [p(SPEAKER), p("M11 6v4"), p("M13.5 4v8")],
    "volume-x": [p(SPEAKER), p("M10.5 6l4 4M14.5 6l-4 4")],
    "audio-lines": [p("M2.5 6.5v3M5.5 4.5v7M8 2v12M10.5 4.5v7M13.5 6.5v3")],
    "music": [p("M6 11V3l7.5-1.5V9.5"), p("M2 10.5h4v3.5H2zM9.5 9h4v3.5h-4z")],
    "bell": [p("M3 11.5v-5L5.5 3h5L13 6.5v5l1.5 1.5h-13z"), p("M6.5 14.5h3")],
    # Time
    "clock": [p(BOX), p("M8 4.5V8h3")],
    "alarm-clock": [p("M2.5 4h11v10.5h-11z"), p("M8 6.5V9.5h2.5"), p("M1.5 2.5l2-1M14.5 2.5l-2-1")],
    "timer": [p("M3.5 1.5h9M3.5 14.5h9"), p("M4.5 1.5V4L8 8l-3.5 4v2.5M11.5 1.5V4L8 8l3.5 4v2.5")],
    "calendar-clock": [
        p("M1.5 3h13v11.5h-13z"),
        thin("M1.5 6.5h13"),
        p("M5 1.5V4.5M11 1.5V4.5"),
        p("M8 8.5v3h3"),
    ],
    "history": [p("M5 2h9v12H2V5"), p("M2 1.5V5h3.5"), p("M8 5v3.5h2.5")],
    # Files and knowledge
    "file": [p(FILE)],
    "file-text": [p(FILE), thin("M5.5 8h5M5.5 10.5h5M5.5 13h3")],
    "file-pen-line": [
        p("M12.5 6.5V5L9.5 1.5H3v13h4"),
        p("M9.5 1.5V5H13"),
        p("M12 8.5l2 2-4 4H8v-2z"),
    ],
    "folder-open": [p(FOLDER), thin("M1.5 7h13")],
    "folder-plus": [p(FOLDER), p("M8 7.5v4.5M5.75 9.75h4.5")],
    "folder-input": [p(FOLDER), p("M4.5 9.5h5.5M8 7.5l2 2-2 2")],
    "book-open": [p("M1.5 3h5L8 4.5 9.5 3h5v10h-5L8 14.5 6.5 13h-5z"), thin("M8 4.5v10")],
    "notebook": [
        p("M3 1.5h10.5v13H3z"),
        thin("M6 1.5v13"),
        p("M1.5 4.5H4.5M1.5 8H4.5M1.5 11.5H4.5"),
    ],
    "library": [p("M2 2v12M5 2v12"), p("M8 3l2.5-1 4 12-2.5 1z")],
    "bookmark": [p("M3.5 1.5h9v13L8 11l-4.5 3.5z")],
    "text-search": [p("M1.5 3h13M1.5 6.5h5.5M1.5 10h3.5"), p("M8.5 8h4v4h-4z"), p("M12.5 12l2 2")],
    "list": [p("M5 4h9.5M5 8h9.5M5 12h9.5"), px(1.5, 3.25), px(1.5, 7.25), px(1.5, 11.25)],
    "list-checks": [
        p("M8 4h6.5M8 8.5h6.5M8 13h6.5"),
        p("M1.5 4l1.5 1.5 3-3"),
        p("M1.5 10.5l1.5 1.5 3-3"),
    ],
    "layers": [
        p("M8 1.5l6.5 3.5L8 8.5 1.5 5z"),
        p("M1.5 8L8 11.5 14.5 8"),
        p("M1.5 11L8 14.5 14.5 11"),
    ],
    "graduation-cap": [p("M8 2.5L15 6 8 9.5 1 6z"), p("M4 7.5v4l4 2 4-2v-4"), p("M15 6v4")],
    "brain": [
        p("M8 4.5L11.5 8 8 11.5 4.5 8z"),
        px(7.25, 7.25, 1.5),
        p("M1.5 1.5h3v3h-3zM11.5 1.5h3v3h-3zM11.5 11.5h3v3h-3z"),
        thin("M4.5 4.5l1.75 1.75M11.5 4.5l-1.75 1.75M11.5 11.5l-1.75-1.75"),
    ],
    # Computer
    "terminal": [p("M1.5 2.5h13v11h-13z"), p("M4 6l2 2-2 2"), p("M7.5 10.5h4.5")],
    "code": [p("M5.5 4L2 8l3.5 4M10.5 4L14 8l-3.5 4"), thin("M9 2.5l-2 11")],
    "cpu": [
        p("M4 4h8v8H4z"),
        px(6.5, 6.5, 3),
        p("M6 1.5V4M10 1.5V4M6 12v2.5M10 12v2.5M1.5 6H4M1.5 10H4M12 6h2.5M12 10h2.5"),
    ],
    "monitor": [p(MONITOR)],
    "monitor-up": [p(MONITOR), p("M8 9V5M6 7l2-2 2 2")],
    "app-window": [
        p("M1.5 2.5h13v11h-13z"),
        thin("M1.5 5.5h13"),
        px(3, 3.25, 1.5),
        px(5.25, 3.25, 1.5),
    ],
    "keyboard": [p("M1 4h14v8.5H1z"), px(3, 6), px(5.5, 6), px(8, 6), px(10.5, 6), thin("M4 10h8")],
    "smartphone": [p("M4 1h8v14H4z"), p("M6.5 12.5h3")],
    "server": [
        p("M1.5 2h13v5h-13zM1.5 9h13v5h-13z"),
        px(11, 3.75),
        px(11, 10.75),
        thin("M3.5 4.5h4M3.5 11.5h4"),
    ],
    "hard-drive": [p("M1.5 9.5h13v5h-13z"), p("M3 9.5L5 2h6l2 7.5"), px(11, 11.25)],
    "hard-drive-download": [
        p("M1.5 9.5h13v5h-13z"),
        p("M8 1.5v5.5M5.5 4.5L8 7l2.5-2.5"),
        px(11, 11.25),
    ],
    "plug": [p("M5.5 1.5v3M10.5 1.5v3"), p("M3.5 4.5h9v3L10 10.5H6L3.5 7.5z"), p("M8 10.5v4")],
    "wifi": [p("M1.5 6.5a9.2 9.2 0 0 1 13 0"), p("M4 9.5a5.6 5.6 0 0 1 8 0"), px(7, 12, 2)],
    "network": [
        p("M6 1.5h4v4H6zM1.5 10.5h4v4h-4zM10.5 10.5h4v4h-4z"),
        p("M8 5.5V8M3.5 10.5V8h9v2.5"),
    ],
    "radar": [ring(8, 8, 6.5), ring(8, 8, 3, faint=True), p("M8 8l4.5-4.5"), px(9.5, 4.5, 1.5)],
    "gauge": [p("M1.5 12a6.5 6.5 0 0 1 13 0"), p("M8 12l3-4.5"), thin("M1.5 14h13")],
    "activity": [p("M3 13.5V9M6.5 13.5V4M10 13.5V7M13.5 13.5V2.5"), thin("M1 14.5h14")],
    "globe": [
        ring(8, 8, 6.5),
        thin("M1.5 8h13"),
        '<ellipse cx="8" cy="8" rx="2.75" ry="6.5" stroke-width="1" opacity=".6"/>',
    ],
    "cloud-sun": [
        p("M3 13.5h9.5L14.5 11.5V10L12.5 8h-1.5L9 5.5H6L4 8H3L1.5 9.5v2.5z"),
        px(11.5, 1.5, 2.5),
        thin("M10 3l-1-1M15 5h-1"),
    ],
    "calculator": [
        p("M3 1.5h10v13H3z"),
        p("M5 4h6v2.5H5z"),
        px(5, 9),
        px(7.25, 9),
        px(9.5, 9),
        px(5, 11.5),
        px(7.25, 11.5),
        px(9.5, 11.5),
    ],
    "sun": [
        p("M5.5 5.5h5v5h-5z"),
        p("M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M13 3l-1.5 1.5M3 13l1.5-1.5M13 13l-1.5-1.5"),
    ],
    "moon": [bevel("M10 1.5a6.5 6.5 0 1 0 4.5 8.5A5 5 0 0 1 10 1.5z")],
    # Work
    "wrench": [p("M2 14l6-6"), p("M7 7V4l2.5-2.5H12L10 3.5 12.5 6l2-2v2.5L12 9H9z")],
    "briefcase": [p("M1.5 4.5h13v9.5h-13z"), p("M5.5 4.5V2h5v2.5"), thin("M1.5 8.5h13")],
    "ticket": [p("M1.5 3.5h13v3L13 8l1.5 1.5v3h-13v-3L3 8 1.5 6.5z"), thin("M10 3.5v9")],
    "workflow": [p("M1.5 1.5h5v5h-5zM9.5 9.5h5v5h-5z"), p("M4 6.5V12h5.5")],
    "flask-conical": [p("M5.5 1.5h5M6.5 1.5V6L2 14.5h12L9.5 6V1.5"), thin("M4 11h8")],
    "git-commit": [p("M1 8h4.5M10.5 8H15"), p("M5.5 5.5h5v5h-5z")],
}


def sprite() -> str:
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg">',
        "<!-- Generated by scripts/glyphs.py: ctOS line glyphs on a 16px grid. -->",
    ]
    for name, parts in sorted(GLYPHS.items()):
        lines.append(f'<symbol id="i-{name}" viewBox="0 0 16 16">{"".join(parts)}</symbol>')
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def sheet(path: Path) -> None:
    """Every glyph at 16px and 48px, the way the UI draws them."""
    cells = "".join(
        f'<div class="c"><svg class="i"><use href="#i-{n}"/></svg>'
        f'<svg class="i big"><use href="#i-{n}"/></svg><span>{n}</span></div>'
        for n in sorted(GLYPHS)
    )
    path.write_text(
        "<!doctype html><meta charset=utf-8><style>"
        "body{background:#0e0e0e;color:#cacaca;font:11px monospace;margin:16px}"
        ".g{display:grid;grid-template-columns:repeat(8,1fr);gap:8px}"
        ".c{display:flex;align-items:center;gap:10px;padding:6px;border:1px solid #2a2a2a}"
        ".i{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:1.5;stroke-linecap:square;stroke-linejoin:miter}"
        ".big{width:48px;height:48px;color:#fff}span{color:#7a7a7a}</style>"
        + sprite()
        + f'<div class="g">{cells}</div>'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sheet", type=Path, help="also write a contact sheet (HTML) here")
    args = parser.parse_args()
    OUT.write_text(sprite(), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(GLYPHS)} glyphs)")
    if args.sheet:
        sheet(args.sheet)
        print(f"Wrote {args.sheet}")


if __name__ == "__main__":
    main()
