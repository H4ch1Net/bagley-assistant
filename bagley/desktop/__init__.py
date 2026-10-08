"""Bagley on the Linux desktop: what is on screen and what is selected.

``bagley see``, ``bagley explain`` and the Quickshell overlay use ``capture`` to read the focused
Hyprland window, a screenshot of it, the highlighted text and the clipboard. The QML for the
overlay and the bar segment lives in ``desktop/quickshell/bagley/`` in the repository.
"""

from __future__ import annotations

from bagley.desktop.capture import (
    ScreenContext,
    Session,
    Unavailable,
    Window,
    clipboard,
    focused_window,
    grab_window,
    looks_secret,
    screen_context,
    selection,
)

__all__ = [
    "ScreenContext",
    "Session",
    "Unavailable",
    "Window",
    "clipboard",
    "focused_window",
    "grab_window",
    "looks_secret",
    "screen_context",
    "selection",
]
