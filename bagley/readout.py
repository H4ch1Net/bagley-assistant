"""Terminal readouts in the ctOS voice: uppercase labels, ``[OK]`` / ``[WARN]`` / ``[CRIT]`` tags
and 24-bit colour from the ctOS palette. Plain text when ``NO_COLOR`` is set or the output is not
a terminal, so scripts can read it.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

PALETTE = {
    "gray": "122;122;122",  # #7A7A7A labels and dividers.
    "body": "202;202;202",  # #CACACA
    "white": "255;255;255",
    "ok": "0;250;154",  # #00FA9A
    "error": "252;62;56",  # #FC3E38
}
TAG_COLOR = {"OK": "ok", "CRIT": "error", "WARN": "white"}


class Readout:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        isatty = getattr(self.stream, "isatty", None)
        self.on = bool(isatty and isatty()) and not os.environ.get("NO_COLOR")

    def paint(self, color: str, text: str) -> str:
        return f"\033[38;2;{PALETTE[color]}m{text}\033[0m" if self.on else text

    def tag(self, level: str) -> str:
        """``[OK]``, ``[WARN]``, ``[CRIT]`` or ``[INFO]``, padded to one width."""
        text = f"[{level.upper()}]"
        return self.paint(TAG_COLOR.get(level.upper(), "gray"), text) + " " * (6 - len(text))

    def print(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def row(self, label: str, value: str = "", level: str | None = None, width: int = 12) -> None:
        """``LABEL       [OK]   value``"""
        tag = self.tag(level) + " " if level else ""
        self.print(f"{self.paint('gray', label.upper().ljust(width))} {tag}{value}")

    def head(self, text: str) -> None:
        self.print(self.paint("white", f"» {text.upper()}"))

    def cmd(self, text: str) -> None:
        """A command for the user to run, indented so it copies cleanly."""
        self.print("  " + self.paint("body", text))

    def note(self, text: str) -> None:
        self.print(self.paint("gray", text))
