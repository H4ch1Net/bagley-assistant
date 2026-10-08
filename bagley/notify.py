"""Notifications beyond the browser: the Linux desktop (``notify-send``, which mako shows on
Hyprland) and the phone (an ntfy topic).

Every notification has a level. ``info`` is routine (a reply, a reminder), ``important`` is
worth a buzz on the phone (an automation result, a morning briefing) and ``critical`` needs
attention now (a failed service, an approval nobody answered). The ntfy setting picks the
lowest level that reaches the phone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from bagley.config import Preferences

log = logging.getLogger("bagley.notify")

LEVELS = ("info", "important", "critical")
NTFY_MIN = {"all": 0, "important": 1, "critical": 2}
NTFY_PRIORITY = {"info": "default", "important": "high", "critical": "urgent"}
URGENCY = {"info": "normal", "important": "normal", "critical": "critical"}
APP_NAME = "Bagley"


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


@dataclass
class Note:
    title: str
    body: str = ""
    level: str = "info"
    url: str = ""  # Opened when the notification is clicked.
    tags: list[str] = field(default_factory=list)
    actions: list[tuple[str, str]] = field(default_factory=list)  # (key, label), desktop only.


def desktop_available() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("notify-send"):
        return False
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("WAYLAND_DISPLAY"))


async def send_desktop(note: Note, *, wait: float = 0) -> str | None:
    """Show ``note`` with notify-send. With ``wait`` and actions, return the chosen action key
    (mako shows actions in its menu, or on click for the default action)."""
    args = [
        "notify-send",
        "--app-name",
        APP_NAME,
        "--urgency",
        URGENCY.get(note.level, "normal"),
        "--expire-time",
        "0" if note.level == "critical" else "8000",
    ]
    for key, label in note.actions:
        args += ["--action", f"{key}={label}"]
    if note.actions and wait:
        args.append("--wait")
    args += [note.title[:120], note.body[:600]]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError as exc:
        log.debug("notify-send failed: %s", exc)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=wait or 5)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    choice = out.decode().strip()
    return choice or None


def ntfy_headers(note: Note, token: str = "") -> dict[str, str]:
    # Header values must be latin-1; ntfy reads RFC 2047 encoded words for anything else.
    def header(text: str) -> str:
        try:
            text.encode("latin-1")
            return text
        except UnicodeEncodeError:
            import base64

            return "=?UTF-8?B?" + base64.b64encode(text.encode()).decode() + "?="

    headers = {
        "Title": header(note.title[:200]),
        "Priority": NTFY_PRIORITY.get(note.level, "default"),
        "Markdown": "yes",
    }
    if note.tags:
        headers["Tags"] = ",".join(note.tags)
    if note.url:
        headers["Click"] = note.url
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def send_ntfy(http: httpx.AsyncClient, url: str, note: Note, token: str = "") -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or len(parts.path) < 2:
        log.warning("ntfy URL must include the topic, e.g. https://ntfy.sh/bagley-abc123")
        return False
    try:
        resp = await http.post(
            url, content=(note.body or note.title).encode(), headers=ntfy_headers(note, token)
        )
    except httpx.HTTPError as exc:
        log.warning("ntfy failed: %s", exc)
        return False
    if resp.status_code >= 400:
        log.warning("ntfy answered %s: %s", resp.status_code, resp.text[:200])
        return False
    return True


async def fan_out(
    prefs: Preferences, http: httpx.AsyncClient, note: Note, *, desktop: bool | None = None
) -> dict[str, Any]:
    """Send ``note`` to the desktop and the phone as the preferences allow."""
    sent: dict[str, Any] = {}
    jobs = []
    if (prefs.desktop_notifications if desktop is None else desktop) and desktop_available():
        jobs.append(("desktop", send_desktop(note)))
    if prefs.ntfy_url and level_rank(note.level) >= NTFY_MIN[prefs.ntfy_level]:
        jobs.append(("phone", send_ntfy(http, prefs.ntfy_url, note, prefs.ntfy_token)))
    results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
    for (name, _), result in zip(jobs, results, strict=True):
        sent[name] = not isinstance(result, BaseException) and result is not False
    return sent
