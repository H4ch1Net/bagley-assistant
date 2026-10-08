"""What the user is looking at on a Wayland desktop (Hyprland): the focused window, a screenshot
of it, the highlighted text and the clipboard.

Every program runs through a ``Session``: ``hyprctl -j activewindow`` for the window, ``grim``
for the screenshot and ``wl-paste`` for the primary selection and the clipboard. A session fills
in ``WAYLAND_DISPLAY`` and ``HYPRLAND_INSTANCE_SIGNATURE`` from ``$XDG_RUNTIME_DIR`` when they are
missing (a systemd unit or an SSH shell lacks them), and tests hand it a fake runner. Off Wayland,
or when a program is missing, the helpers raise ``Unavailable`` with a short reason;
``screen_context`` collects those reasons instead of failing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

MAX_TEXT = 8000  # Characters of selected or copied text to send.
MAX_IMAGE = 11_000_000  # The server accepts images up to 12 MB.
TIMEOUT = 5.0
SHOT_TIMEOUT = 10.0
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
# Text targets in the order wl-paste should prefer them.
TEXT_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "TEXT", "STRING")
PASSWORD_HINT = "x-kde-passwordmanagerhint"  # KeePassXC and others mark copied passwords.
GEOMETRY = re.compile(r"^-?\d{1,6},-?\d{1,6} (\d{1,5})x(\d{1,5})$")
OUTPUT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SECRETS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{30,}"
    r"|\bglpat-[A-Za-z0-9_-]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{35}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)


class Unavailable(Exception):
    """This capture can't be made here: not a Wayland session, a program is missing..."""


@dataclass
class Result:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


Runner = Callable[[list[str], Mapping[str, str], float], Result]


def run(args: list[str], env: Mapping[str, str], timeout: float) -> Result:
    """Run a program (never through a shell) and collect its output."""
    try:
        proc = subprocess.run(
            args,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Unavailable(f"{args[0].upper()} TIMED OUT") from exc
    except OSError as exc:
        raise Unavailable(f"{args[0].upper()} FAILED: {exc.strerror or exc}") from exc
    return Result(proc.returncode, proc.stdout or b"", proc.stderr or b"")


# Session ----------------------------------------------------------------------------------------


def runtime_dir(env: Mapping[str, str]) -> Path:
    if env.get("XDG_RUNTIME_DIR"):
        return Path(env["XDG_RUNTIME_DIR"])
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(f"/run/user/{uid}")


def _newest(paths: list[tuple[Path, Path]]) -> str:
    """Name of the entry whose marker file changed last ("" when none exists)."""
    found = []
    for entry, marker in paths:
        with contextlib.suppress(OSError):
            found.append((marker.stat().st_mtime, entry.name))
    return max(found)[1] if found else ""


def find_hyprland(run_dir: Path, legacy: Path = Path("/tmp/hypr")) -> str:
    """Signature of the newest Hyprland instance, read from its socket folder
    (``$XDG_RUNTIME_DIR/hypr/<signature>/.socket.sock``; ``/tmp/hypr`` before Hyprland 0.40)."""
    for base in (run_dir / "hypr", legacy):
        try:
            entries = [e for e in base.iterdir() if e.is_dir()]
        except OSError:
            continue
        if name := _newest([(e, e / ".socket.sock") for e in entries]):
            return name
    return ""


def find_wayland(run_dir: Path) -> str:
    """Name of the newest Wayland socket in the runtime folder (``wayland-1`` on Hyprland)."""
    try:
        sockets = [p for p in run_dir.glob("wayland-*") if not p.name.endswith(".lock")]
    except OSError:
        return ""
    return _newest([(p, p) for p in sockets])


@dataclass
class Session:
    """How to reach the desktop: the environment its programs run with and the function that
    runs them. ``Session.detect()`` builds one for this process."""

    env: dict[str, str] = field(default_factory=dict)
    runner: Runner = run
    which: Callable[[str], str | None] = shutil.which

    @classmethod
    def detect(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        runner: Runner = run,
        which: Callable[[str], str | None] = shutil.which,
        legacy_hypr: Path = Path("/tmp/hypr"),
    ) -> Session:
        env = dict(os.environ if env is None else env)
        run_dir = runtime_dir(env)
        if not env.get("WAYLAND_DISPLAY") and (name := find_wayland(run_dir)):
            env["WAYLAND_DISPLAY"] = name
            env.setdefault("XDG_RUNTIME_DIR", str(run_dir))
        if not env.get("HYPRLAND_INSTANCE_SIGNATURE") and (
            signature := find_hyprland(run_dir, legacy_hypr)
        ):
            env["HYPRLAND_INSTANCE_SIGNATURE"] = signature
            env.setdefault("XDG_RUNTIME_DIR", str(run_dir))
        return cls(env, runner, which)

    def call(self, args: list[str], timeout: float = TIMEOUT) -> Result:
        program = args[0]
        if program == "hyprctl":
            if not self.env.get("HYPRLAND_INSTANCE_SIGNATURE"):
                raise Unavailable("HYPRLAND NOT RUNNING")
        elif not self.env.get("WAYLAND_DISPLAY"):
            raise Unavailable("NO WAYLAND SESSION")
        if not self.which(program):
            raise Unavailable(f"{program.upper()} NOT INSTALLED")
        return self.runner(args, self.env, timeout)


def _first_line(data: bytes) -> str:
    lines = data.decode("utf-8", "replace").strip().splitlines()
    return lines[0][:120] if lines else "no output"


# Focused window ---------------------------------------------------------------------------------


@dataclass
class Window:
    """The focused window as Hyprland reports it. ``app`` is its class (``kitty``, ``firefox``)."""

    title: str
    app: str
    x: int
    y: int
    width: int
    height: int
    address: str = ""
    monitor: int | None = None

    @property
    def geometry(self) -> str:
        """The window's rectangle in grim's ``X,Y WxH`` form (layout coordinates)."""
        return f"{self.x},{self.y} {self.width}x{self.height}"


def _pair(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return None
    return round(value[0]), round(value[1])


def parse_window(raw: bytes | str) -> Window | None:
    """Read ``hyprctl -j activewindow``. Hyprland prints ``{}`` when no window has focus."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        raise Unavailable(f"HYPRCTL: {text.strip()[:120] or 'no output'}") from exc
    if not isinstance(data, dict) or not data:
        return None
    at, size = _pair(data.get("at")), _pair(data.get("size"))
    if at is None or size is None:
        return None
    monitor = data.get("monitor")
    return Window(
        title=str(data.get("title") or "")[:300],
        app=str(data.get("class") or data.get("initialClass") or "")[:100],
        x=at[0],
        y=at[1],
        width=size[0],
        height=size[1],
        address=str(data.get("address") or ""),
        monitor=monitor if isinstance(monitor, int) and not isinstance(monitor, bool) else None,
    )


def focused_window(session: Session | None = None) -> Window | None:
    """The focused window, or None when nothing has focus. A layer surface such as the Bagley
    overlay never counts as the focused window, so this still names the app underneath."""
    session = session or Session.detect()
    result = session.call(["hyprctl", "-j", "activewindow"])
    if result.returncode != 0:
        raise Unavailable(f"HYPRCTL: {_first_line(result.stderr or result.stdout)}")
    return parse_window(result.stdout)


def focused_output(session: Session) -> str:
    """Name of the focused monitor (``eDP-1``), or "" when Hyprland doesn't say."""
    result = session.call(["hyprctl", "-j", "monitors"])
    try:
        monitors = json.loads(result.stdout)
    except ValueError:
        return ""
    for monitor in monitors if isinstance(monitors, list) else []:
        if not isinstance(monitor, dict) or monitor.get("focused") is not True:
            continue
        name = monitor.get("name")
        if isinstance(name, str) and OUTPUT_NAME.match(name):
            return name
    return ""


# Screenshot -------------------------------------------------------------------------------------


def _shoot(session: Session, where: list[str]) -> bytes | None:
    """One grim capture as PNG, or as JPEG when the PNG is too big to send."""
    result = session.call(["grim", *where, "-"], timeout=SHOT_TIMEOUT)
    data = result.stdout
    if result.returncode != 0 or not data.startswith(PNG):
        return None
    if len(data) <= MAX_IMAGE:
        return data
    result = session.call(["grim", "-t", "jpeg", "-q", "85", *where, "-"], timeout=SHOT_TIMEOUT)
    data = result.stdout
    if result.returncode != 0 or not data.startswith(JPEG) or len(data) > MAX_IMAGE:
        return None
    return data


def capture(target: Window | str | None, session: Session) -> tuple[bytes, str]:
    """A screenshot of ``target`` (a window or an ``X,Y WxH`` rectangle) and what it shows:
    ``window``, else the focused ``output``, else the whole ``screen``."""
    geometry = (target.geometry if isinstance(target, Window) else target) or ""
    match = GEOMETRY.match(geometry)
    sane = match is not None and all(int(n) > 0 for n in match.groups())
    if sane and (data := _shoot(session, ["-g", geometry])):
        return data, "window"
    output = ""
    with contextlib.suppress(Unavailable):
        output = focused_output(session)
    if output and (data := _shoot(session, ["-o", output])):
        return data, "output"
    if data := _shoot(session, []):
        return data, "screen"
    raise Unavailable("GRIM FAILED")


def grab_window(target: Window | str | None = None, session: Session | None = None) -> bytes:
    """PNG bytes of the focused window (``target``), falling back to the focused output and then
    to every output. JPEG only when a PNG would be too large for the server."""
    return capture(target, session or Session.detect())[0]


# Selection and clipboard ------------------------------------------------------------------------


def _paste(session: Session, primary: bool) -> str:
    base = ["wl-paste", "--primary"] if primary else ["wl-paste"]
    what = "SELECTION" if primary else "CLIPBOARD"
    listed = session.call([*base, "--list-types"])
    if listed.returncode != 0:
        return ""  # Nothing selected or copied.
    types = [t.strip() for t in listed.stdout.decode("utf-8", "replace").splitlines() if t.strip()]
    if any(t.lower() == PASSWORD_HINT for t in types):
        raise Unavailable(f"{what} SKIPPED: PASSWORD MANAGER")
    target = next((t for t in TEXT_TYPES if t in types), None)
    target = target or next((t for t in types if t.startswith("text/")), None)
    if target is None:
        if types:
            raise Unavailable(f"{what} NOT TEXT ({types[0][:40]})")
        return ""
    result = session.call([*base, "--no-newline", "--type", target])
    if result.returncode != 0:
        return ""
    text = result.stdout[: MAX_TEXT * 4].decode("utf-8", "replace").replace("\x00", "")
    if text.count("�") > max(2, len(text) // 100):
        raise Unavailable(f"{what} NOT TEXT (binary)")
    return text[:MAX_TEXT]


def selection(session: Session | None = None) -> str:
    """The highlighted text (the primary selection), at most 8000 characters; "" when none."""
    return _paste(session or Session.detect(), primary=True)


def clipboard(session: Session | None = None) -> str:
    """The clipboard when it holds text, at most 8000 characters; "" when empty."""
    return _paste(session or Session.detect(), primary=False)


def looks_secret(text: str) -> bool:
    """True for text that looks like a key, a token or a generated password. Such clipboard
    contents are left out of the context nobody asked to send."""
    if SECRETS.search(text):
        return True
    token = text.strip()
    if not 8 <= len(token) <= 64 or any(c.isspace() for c in token) or "://" in token:
        return False
    return (
        any(c.islower() for c in token)
        and any(c.isupper() for c in token)
        and any(c.isdigit() for c in token)
        and any(not c.isalnum() for c in token)
    )


# Everything at once -----------------------------------------------------------------------------


class ScreenContext(NamedTuple):
    image: bytes | None  # PNG (JPEG when large) of the focused window.
    context: dict[str, str]  # window_title, app, selection, clipboard (the /api/ask keys).
    notes: list[str]  # Why something is missing, e.g. "GRIM NOT INSTALLED".


def screen_context(
    session: Session | None = None, *, image: bool = True, clipboard_text: bool = True
) -> ScreenContext:
    """The focused window's screenshot plus its title, app class, the highlighted text and the
    clipboard. Whatever can't be read is left out and explained in ``notes``."""
    session = session or Session.detect()
    notes: list[str] = []
    context: dict[str, str] = {}
    shot: bytes | None = None

    def note(text: str) -> None:
        if text not in notes:
            notes.append(text)

    window = None
    try:
        window = focused_window(session)
    except Unavailable as exc:
        note(str(exc))
    if window:
        context["window_title"] = window.title
        context["app"] = window.app
    if image:
        try:
            shot, scope = capture(window, session)
            if scope != "window":
                note("CAPTURED THE FOCUSED OUTPUT" if scope == "output" else "CAPTURED ALL OUTPUTS")
        except Unavailable as exc:
            note(str(exc))
    selected = ""
    try:
        selected = selection(session)
    except Unavailable as exc:
        note(str(exc))
    if selected.strip():
        context["selection"] = selected
    if clipboard_text:
        try:
            copied = clipboard(session)
        except Unavailable as exc:
            note(str(exc))
            copied = ""
        if copied.strip() and copied.strip() != selected.strip():
            if looks_secret(copied):
                note("CLIPBOARD SKIPPED: LOOKS LIKE A SECRET")
            else:
                context["clipboard"] = copied
    return ScreenContext(shot, {k: v for k, v in context.items() if v}, notes)


async def screen_context_async(
    session: Session | None = None, *, image: bool = True, clipboard_text: bool = True
) -> ScreenContext:
    """``screen_context`` off the event loop, for server code."""
    return await asyncio.to_thread(
        screen_context, session, image=image, clipboard_text=clipboard_text
    )
