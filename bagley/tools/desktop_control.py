"""Control the Hyprland desktop: windows, workspaces, apps, media, volume, Wi-Fi and the power
profile.

Reading the desktop's state is free; every change asks first. Each tool runs one program with an
argument list (``hyprctl``, ``playerctl``, ``wpctl``, ``nmcli``, ``powerprofilesctl``) through
``desktop.runner``, never a shell string from the model, so tests swap in a fake. Apps open
through Hyprland (``hyprctl dispatch exec``) and only from an allowlist: the common apps in
``APPS`` plus the applications installed on this computer (their ``.desktop`` entries). Terminal
programs open in kitty with the ctOS window class, ``kitty --class ctos-term -e <app>``.

Bagley often runs as a systemd user service, which does not inherit
``HYPRLAND_INSTANCE_SIGNATURE``. ``find_hyprland`` then picks the newest instance socket under
``$XDG_RUNTIME_DIR/hypr`` and passes it to ``hyprctl``.

Scenes ("set up coding mode") are saved routines; see ``bagley.routines``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from bagley import routines
from bagley.policy import reads_private
from bagley.toolroute import register_group
from bagley.tools import ToolContext, ToolError, tool

TIMEOUT = 8.0  # Seconds one program may take.
MAX_OUTPUT = 200_000
SINK = "@DEFAULT_AUDIO_SINK@"
TERMINAL = ("kitty", "--class", "ctos-term", "-e")  # How ctOS opens terminal programs.
ADDRESS = re.compile(r"0x[0-9a-fA-F]{1,16}")
MAX_VOLUME = 150
LEGACY_SOCKETS = Path("/tmp/hypr")  # Where Hyprland before 0.40 kept its sockets.

# Apps Bagley may open by name: (programs to try in order, runs inside a terminal).
APPS: dict[str, tuple[tuple[str, ...], bool]] = {
    "kitty": (("kitty",), False),
    "zed": (("zeditor", "zed"), False),  # Arch packages Zed as zeditor.
    "zeditor": (("zeditor", "zed"), False),
    "firefox": (("firefox",), False),
    "chromium": (("chromium", "chromium-browser"), False),
    "thunar": (("thunar",), False),
    "nautilus": (("nautilus",), False),
    "dolphin": (("dolphin",), False),
    "obsidian": (("obsidian",), False),
    "spotify": (("spotify", "spotify-launcher"), False),
    "code": (("code",), False),
    "discord": (("discord",), False),
    "thunderbird": (("thunderbird",), False),
    "pavucontrol": (("pavucontrol",), False),
    "lazygit": (("lazygit",), True),
    "btop": (("btop",), True),
    "htop": (("htop",), True),
    "nvim": (("nvim",), True),
    "yazi": (("yazi",), True),
}

# What to install when a program is missing (Arch package names).
PACKAGES = {
    "hyprctl": "hyprland",
    "playerctl": "playerctl",
    "wpctl": "wireplumber",
    "nmcli": "networkmanager",
    "powerprofilesctl": "power-profiles-daemon",
    "kitty": "kitty",
}

register_group(
    "desktop",
    "control this desktop: windows, workspaces, apps, media, volume, Wi-Fi, power profile",
    r"\b(window|workspace|open|launch|start|close|move|focus|music|media|play|pause|skip|next"
    r"|volume|mute|louder|quieter|wi-?fi|network|connect|power profile|performance"
    r"|battery saver|coding mode|scene|set ?up)",
)
reads_private("list_windows", "desktop_status")


def available(config: Any) -> bool:
    return sys.platform.startswith("linux")


# Running programs ---------------------------------------------------------------------------


@dataclass
class Result:
    code: int
    out: str = ""
    err: str = ""


# Runs ``argv`` (no shell) with ``env`` (None keeps this process's) and a timeout in seconds.
Runner = Callable[[list[str], dict[str, str] | None, float], Result]


def run_program(argv: list[str], env: dict[str, str] | None, timeout: float) -> Result:
    """Run a program without a shell and capture its output. The default runner."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{argv[0]} did not answer within {timeout:g}s.") from exc
    except OSError as exc:
        raise ToolError(f"Could not run {argv[0]}: {exc.strerror or exc}.") from exc
    return Result(proc.returncode, proc.stdout[:MAX_OUTPUT], proc.stderr[:MAX_OUTPUT])


def find_hyprland(env: Mapping[str, str]) -> tuple[str, str | None] | None:
    """The running Hyprland instance as (signature, runtime dir for ``hyprctl`` or None).

    Taken from the environment when Hyprland started this process. Otherwise (a systemd user
    service) the newest instance directory under ``$XDG_RUNTIME_DIR/hypr`` that has a
    ``.socket.sock`` wins; ``/tmp/hypr`` is checked for Hyprland before 0.40.
    """
    signature = (env.get("HYPRLAND_INSTANCE_SIGNATURE") or "").strip()
    if signature:
        return signature, None
    runtime = env.get("XDG_RUNTIME_DIR") or ""
    if not runtime and hasattr(os, "getuid"):
        runtime = f"/run/user/{os.getuid()}"
    roots: list[tuple[Path, str | None]] = [(Path(runtime) / "hypr", runtime)] if runtime else []
    roots.append((LEGACY_SOCKETS, None))
    best: tuple[float, str, str | None] | None = None
    for root, runtime_dir in roots:
        try:
            instances = list(root.iterdir())
        except OSError:
            continue
        for instance in instances:
            try:
                mtime = (instance / ".socket.sock").stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, instance.name, runtime_dir)
    return (best[1], best[2]) if best else None


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:300]


class Desktop:
    """Runs the desktop's programs. Tests replace ``runner``, ``which`` and ``env``."""

    def __init__(
        self,
        runner: Runner = run_program,
        which: Callable[[str], str | None] = shutil.which,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.runner = runner
        self.which = which
        self.env = os.environ if env is None else env

    def require(self, program: str) -> None:
        if not self.which(program):
            package = PACKAGES.get(program)
            hint = f" (package: {package})" if package else ""
            raise ToolError(f"{program} is not installed{hint}.")

    def call(self, program: str, *args: str, timeout: float = TIMEOUT) -> Result:
        """Run ``program`` and return its result, whatever its exit code."""
        self.require(program)
        env = self.hyprland_env() if program == "hyprctl" else None
        return self.runner([program, *args], env, timeout)

    def run(self, program: str, *args: str, timeout: float = TIMEOUT) -> str:
        """Run ``program`` and return its output; a non-zero exit raises ``ToolError``."""
        result = self.call(program, *args, timeout=timeout)
        if result.code != 0:
            reason = _first_line(result.err or result.out) or f"exit code {result.code}"
            raise ToolError(f"{program} failed: {reason}")
        return result.out

    def hyprland_env(self) -> dict[str, str]:
        found = find_hyprland(self.env)
        if found is None:
            raise ToolError(
                "Hyprland is not running (no instance socket in $XDG_RUNTIME_DIR/hypr)."
            )
        signature, runtime_dir = found
        env = dict(self.env)
        env["HYPRLAND_INSTANCE_SIGNATURE"] = signature
        if runtime_dir and not env.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = runtime_dir
        return env

    def hypr_json(self, what: str) -> Any:
        out = self.run("hyprctl", "-j", what)
        try:
            return json.loads(out or "null")
        except json.JSONDecodeError as exc:
            raise ToolError(f"hyprctl gave an unexpected answer for {what}.") from exc

    def dispatch(self, *args: str) -> None:
        """``hyprctl dispatch``; it prints "ok" on success and the reason otherwise."""
        out = self.run("hyprctl", "dispatch", *args).strip()
        if out and out.lower() != "ok":
            raise ToolError(f"Hyprland refused: {_first_line(out)}")


desktop = Desktop()


# Reading the desktop ------------------------------------------------------------------------


def _windows() -> list[dict[str, Any]]:
    """Open windows, the most recently focused first."""
    clients = desktop.hypr_json("clients")
    if not isinstance(clients, list):
        return []
    clients = [c for c in clients if isinstance(c, dict) and c.get("mapped", True)]
    clients.sort(key=lambda c: c.get("focusHistoryID", 999))
    out = []
    for c in clients:
        address = str(c.get("address") or "")
        if not ADDRESS.fullmatch(address):
            continue
        workspace = c.get("workspace") or {}
        out.append(
            {
                "address": address,
                "class": str(c.get("class") or c.get("initialClass") or ""),
                "title": str(c.get("title") or "")[:200],
                "workspace": workspace.get("id"),
                "focused": c.get("focusHistoryID") == 0,
            }
        )
    return out


def find_window(match: str, *, unique: bool = False) -> dict[str, Any]:
    """The window whose address, class or title matches (in that order of preference).
    ``unique`` refuses to guess between several windows."""
    query = " ".join(match.split())
    if not query:
        raise ToolError("Say which window: part of its class or title, or its address.")
    windows = _windows()
    if ADDRESS.fullmatch(query):
        hit = next((w for w in windows if w["address"].lower() == query.lower()), None)
        if hit is None:
            raise ToolError(f"No window has the address {query}.")
        return hit
    q = query.lower()
    tiers = (
        [w for w in windows if w["class"].lower() == q],
        [w for w in windows if q in w["class"].lower()],
        [w for w in windows if q in w["title"].lower()],
    )
    for hits in tiers:
        if not hits:
            continue
        if unique and len(hits) > 1:
            listing = "; ".join(
                f"{w['class']} “{w['title'][:40]}” {w['address']}" for w in hits[:6]
            )
            raise ToolError(
                f"{len(hits)} windows match '{query}': {listing}. Use the address to pick one."
            )
        return hits[0]
    raise ToolError(f"No open window matches '{query}'. list_windows shows them.")


def _workspace(n: int) -> int:
    if not 1 <= n <= 10:
        raise ToolError("Workspaces are numbered 1 to 10.")
    return n


def _active_workspace() -> dict[str, Any] | None:
    data = desktop.hypr_json("activeworkspace")
    if not isinstance(data, dict) or "id" not in data:
        return None
    return {"id": data["id"], "name": data.get("name"), "windows": data.get("windows")}


def _focused_window() -> dict[str, Any] | None:
    data = desktop.hypr_json("activewindow")
    if not isinstance(data, dict) or not data.get("address"):
        return None
    return {
        "class": data.get("class") or "",
        "title": str(data.get("title") or "")[:200],
        "address": data["address"],
    }


def _players() -> list[dict[str, str]]:
    fmt = "{{playerName}}\t{{status}}\t{{artist}}\t{{title}}"
    result = desktop.call("playerctl", "-a", "metadata", "--format", fmt)
    if result.code != 0:
        if "no players" in (result.err + result.out).lower():
            return []
        raise ToolError(f"playerctl failed: {_first_line(result.err or result.out)}")
    players = []
    for line in result.out.splitlines():
        parts = [*line.split("\t"), "", "", ""][:4]
        if parts[0]:
            players.append(
                {
                    "player": parts[0],
                    "status": parts[1],
                    "artist": parts[2][:120],
                    "title": parts[3][:120],
                }
            )
    return players


def _volume_state() -> dict[str, Any]:
    out = desktop.run("wpctl", "get-volume", SINK)
    m = re.search(r"Volume:\s*([\d.]+)", out)
    if not m:
        raise ToolError(f"wpctl gave an unexpected answer: {_first_line(out)}")
    return {"percent": round(float(m.group(1)) * 100), "muted": "[MUTED]" in out}


def _terse(line: str) -> list[str]:
    """Split a line of ``nmcli -t`` output, where ':' and '\\' inside values are escaped."""
    fields, buf, escaped = [], [], False
    for ch in line:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    fields.append("".join(buf))
    return fields


def _wifi_state() -> dict[str, Any]:
    out = desktop.run(
        "nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list", "--rescan", "no"
    )
    for line in out.splitlines():
        fields = _terse(line)
        if len(fields) >= 3 and fields[0] == "yes":
            signal = int(fields[2]) if fields[2].isdigit() else None
            return {"connected": True, "ssid": fields[1], "signal": signal}
    return {"connected": False, "ssid": None, "signal": None}


def _power_state() -> str:
    return desktop.run("powerprofilesctl", "get").strip()


@tool(category="desktop", summary="List windows", timeout=20)
def list_windows() -> list[dict[str, Any]]:
    """List the open windows on the user's Hyprland desktop: address, app class, title,
    workspace and which one has focus (most recently focused first)."""
    return _windows()


@tool(category="desktop", summary="Check the desktop", timeout=20)
async def desktop_status() -> dict[str, Any]:
    """What the desktop is doing: active workspace, focused window, media players, volume,
    Wi-Fi and the power profile. Parts whose program is missing are listed as unavailable."""
    parts: dict[str, Callable[[], Any]] = {
        "workspace": _active_workspace,
        "focused": _focused_window,
        "media": _players,
        "volume": _volume_state,
        "wifi": _wifi_state,
        "power_profile": _power_state,
    }
    results = await asyncio.gather(
        *(asyncio.to_thread(read) for read in parts.values()), return_exceptions=True
    )
    status: dict[str, Any] = {}
    unavailable: dict[str, str] = {}
    for key, value in zip(parts, results, strict=True):
        if isinstance(value, ToolError):
            status[key] = None
            unavailable[key] = str(value)
        elif isinstance(value, BaseException):
            raise value
        else:
            status[key] = value
    if unavailable:
        status["unavailable"] = unavailable
    return status


# Changing the desktop -----------------------------------------------------------------------


@tool(category="desktop", risk="confirm", summary="Switch to workspace {workspace}", timeout=20)
def switch_workspace(workspace: Annotated[int, "Workspace number, 1-10"]) -> str:
    """Switch the desktop to another workspace. Asks the user first."""
    desktop.dispatch("workspace", str(_workspace(workspace)))
    return f"Switched to workspace {workspace}."


@tool(category="desktop", risk="confirm", summary="Focus {match}", timeout=20)
def focus_window(
    match: Annotated[str, "Part of the window's app class or title, or its address"],
) -> str:
    """Bring a window to the front and focus it. Asks the user first."""
    window = find_window(match)
    desktop.dispatch("focuswindow", f"address:{window['address']}")
    return f"Focused {window['class']} “{window['title'][:60]}”."


@tool(
    category="desktop", risk="confirm", summary="Move {match} to workspace {workspace}", timeout=20
)
def move_window(
    match: Annotated[str, "Part of the window's app class or title, or its address"],
    workspace: Annotated[int, "Workspace number, 1-10"],
) -> str:
    """Move a window to another workspace without following it. Asks the user first."""
    target = _workspace(workspace)
    window = find_window(match, unique=True)
    desktop.dispatch("movetoworkspacesilent", f"{target},address:{window['address']}")
    return f"Moved {window['class']} to workspace {target}."


@tool(category="desktop", risk="confirm", summary="Close {match}", timeout=20)
def close_window(
    match: Annotated[str, "Part of the window's app class or title, or its address"],
) -> str:
    """Close a window (the app may ask to save). Several matching windows are refused rather
    than guessed. Asks the user first."""
    window = find_window(match, unique=True)
    desktop.dispatch("closewindow", f"address:{window['address']}")
    return f"Closed {window['class']} “{window['title'][:60]}”."


def _exec_line(value: str) -> str:
    """A ``.desktop`` Exec value without its field codes (%f, %U...); ``%%`` is a literal %."""

    def code(m: re.Match[str]) -> str:
        return "%" if m.group(1) == "%" else "" if m.group(1) in "fFuUdDnNickvm" else m.group(0)

    return " ".join(re.sub(r"%(.)", code, value).split())


def _read_entry(path: Path) -> tuple[str, str] | None:
    """(Name, command) of an application's ``.desktop`` file, or None to skip it."""
    try:
        if path.stat().st_size > 64_000:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields: dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
        elif section == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            fields.setdefault(key.strip(), value.strip())
    if fields.get("Type", "Application") != "Application":
        return None
    if fields.get("Hidden") == "true" or fields.get("NoDisplay") == "true":
        return None
    command = _exec_line(fields.get("Exec", ""))
    if not command or not fields.get("Name") or len(command) > 1000:
        return None
    if fields.get("Terminal") == "true":
        command = f"{shlex.join(TERMINAL)} {command}"
    return fields["Name"], command


def desktop_apps(env: Mapping[str, str]) -> dict[str, str]:
    """Applications installed on this computer, from their ``.desktop`` entries: lower-case
    desktop id and name → the command Hyprland runs. A user's entry hides a system one."""
    home = Path(env.get("HOME") or Path.home())
    data_home = env.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    data_dirs = env.get("XDG_DATA_DIRS") or os.pathsep.join(["/usr/local/share", "/usr/share"])
    bases = [data_home, *data_dirs.split(os.pathsep)]
    bases += [str(Path(data_home) / "flatpak/exports/share"), "/var/lib/flatpak/exports/share"]
    apps: dict[str, str] = {}
    seen: set[str] = set()
    for base in filter(None, bases):
        folder = Path(base) / "applications"
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.desktop"))[:2000]:
            desktop_id = path.relative_to(folder).as_posix()[: -len(".desktop")].replace("/", "-")
            if desktop_id in seen:
                continue
            seen.add(desktop_id)
            entry = _read_entry(path)
            if entry is None:
                continue
            name, command = entry
            apps.setdefault(desktop_id.lower(), command)
            apps.setdefault(name.lower(), command)
    return apps


def app_command(name: str) -> str:
    """The command line Hyprland runs for an allowed app, or ``ToolError``."""
    key = " ".join(name.split()).lower()
    if not key:
        raise ToolError("Say which app to open.")
    if key in APPS:
        programs, in_terminal = APPS[key]
        program = next((p for p in programs if desktop.which(p)), None)
        if program is not None:
            if not in_terminal:
                return program
            desktop.require("kitty")
            return shlex.join([*TERMINAL, program])
    installed = desktop_apps(desktop.env)
    if key in installed:
        return installed[key]
    if key in APPS:
        raise ToolError(f"{name} is not installed.")
    raise ToolError(
        f"'{name}' is not an app Bagley may open. Use one of {', '.join(sorted(APPS))}, "
        "or the name of an application installed on this computer."
    )


@tool(category="desktop", risk="confirm", summary="Open {command}", timeout=20)
def launch_app(
    command: Annotated[
        str,
        "App to open, by name only: kitty, zed, firefox, obsidian, spotify, lazygit, btop, nvim... "
        "or an installed application's name",
    ],
    workspace: Annotated[int | None, "Workspace 1-10 to open it on, without switching"] = None,
) -> str:
    """Open an app on the user's desktop, optionally on a given workspace. Only known apps and
    applications installed on this computer can be opened, without arguments; terminal programs
    (lazygit, btop, nvim) open in kitty. Asks the user first."""
    line = app_command(command)
    rule = f"[workspace {_workspace(workspace)} silent] " if workspace is not None else ""
    desktop.dispatch("exec", rule + line)
    where = f" on workspace {workspace}" if workspace is not None else ""
    return f"Opened {' '.join(command.split())}{where}."


@tool(category="desktop", risk="confirm", summary="Media: {action}", timeout=20)
def media(action: Literal["play", "pause", "play-pause", "next", "previous", "stop"]) -> str:
    """Control the music or video that is playing (Spotify, a browser, mpv...). Asks the user
    first."""
    result = desktop.call("playerctl", action)
    if result.code != 0:
        reason = _first_line(result.err or result.out)
        if "no players" in reason.lower():
            raise ToolError("No media player is running.")
        raise ToolError(f"playerctl failed: {reason or f'exit code {result.code}'}")
    return f"Media: {action}."


@tool(category="desktop", risk="confirm", summary="Change the volume", timeout=20)
def volume(
    level: Annotated[int | None, "Set the volume to this percentage (0-150)"] = None,
    change: Annotated[int | None, "Or change it by this many points, e.g. 10 or -10"] = None,
    mute: Annotated[bool | None, "Mute (true) or unmute (false)"] = None,
) -> str:
    """Set, raise or lower the output volume, or mute it. Asks the user first."""
    if level is None and change is None and mute is None:
        raise ToolError("Give a level, a change or mute.")
    if level is not None and change is not None:
        raise ToolError("Give either a level or a change, not both.")
    limit = ("-l", str(MAX_VOLUME / 100))
    if level is not None:
        level = max(0, min(MAX_VOLUME, level))
        desktop.run("wpctl", "set-volume", *limit, SINK, f"{level}%")
    elif change:
        step = max(-MAX_VOLUME, min(MAX_VOLUME, change))
        sign = "+" if step > 0 else "-"
        desktop.run("wpctl", "set-volume", *limit, SINK, f"{abs(step)}%{sign}")
    if mute is not None:
        desktop.run("wpctl", "set-mute", SINK, "1" if mute else "0")
    state = _volume_state()
    return f"Volume {state['percent']}%{' (muted)' if state['muted'] else ''}."


def _saved_networks() -> list[str]:
    out = desktop.run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
    names = []
    for line in out.splitlines():
        fields = _terse(line)
        if len(fields) >= 2 and fields[1] in ("802-11-wireless", "wifi"):
            names.append(fields[0])
    return names


@tool(category="desktop", risk="confirm", summary="Wi-Fi: {action}", timeout=60)
def wifi(
    action: Literal["on", "off", "connect", "disconnect"],
    ssid: Annotated[str, "Saved network to connect to"] = "",
) -> str:
    """Turn Wi-Fi on or off, connect to a network saved on this computer, or disconnect. Bagley
    never handles Wi-Fi passwords: new networks are joined in the system's settings. Asks the
    user first."""
    if action in ("on", "off"):
        desktop.run("nmcli", "radio", "wifi", action)
        return f"Wi-Fi {action}."
    if action == "connect":
        wanted = ssid.strip()
        if not wanted:
            raise ToolError("Say which saved network to connect to.")
        saved = _saved_networks()
        name = next((n for n in saved if n == wanted), None) or next(
            (n for n in saved if n.lower() == wanted.lower()), None
        )
        if name is None:
            listed = ", ".join(saved[:20]) or "none"
            raise ToolError(
                f"'{wanted}' is not a saved network (saved: {listed}). Join new networks in the "
                "system's network settings."
            )
        desktop.run("nmcli", "connection", "up", "id", name, timeout=45)
        return f"Connected to {name}."
    out = desktop.run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device")
    for line in out.splitlines():
        fields = _terse(line)
        if len(fields) >= 3 and fields[1] == "wifi" and fields[2].startswith("connected"):
            desktop.run("nmcli", "device", "disconnect", fields[0])
            return f"Disconnected {fields[0]} from Wi-Fi."
    return "Wi-Fi is not connected."


@tool(category="desktop", risk="confirm", summary="Power profile: {profile}", timeout=20)
def power_profile(profile: Literal["power-saver", "balanced", "performance"]) -> str:
    """Switch the power profile: power-saver for battery life, balanced, or performance.
    Asks the user first."""
    desktop.run("powerprofilesctl", "set", profile)
    return f"Power profile: {profile}."


@tool(category="desktop", risk="confirm", summary="Set up the {name} scene", timeout=180)
async def set_up_scene(
    ctx: ToolContext, name: Annotated[str, "Scene to set up, e.g. 'coding'"]
) -> str:
    """Arrange the desktop for an activity ("set up coding mode"): a scene opens its apps on
    their workspaces and switches there. Scenes are saved routines the user can edit; the
    built-in "coding" scene opens kitty, Zed and lazygit on workspace 2. Asks the user first."""
    rt = ctx.runtime
    if rt is None:
        raise ToolError("Scenes are not available here.")
    routine = routines.find_scene(rt, name)
    if routine is None:
        names = ", ".join(r["name"] for r in routines.list_routines(rt)) or "none"
        raise ToolError(f"There is no scene called '{name}'. Saved routines: {names}.")
    try:
        result = await routines.run_routine(
            rt, routine, source="chat", conversation_id=ctx.conversation_id
        )
    except routines.RoutineError as exc:
        raise ToolError(str(exc)) from exc
    if not result["ok"]:
        raise ToolError(result["text"])
    return result["text"]
