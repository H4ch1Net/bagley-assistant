"""Run Bagley in the background as a systemd user service, and the env file it reads.

``bagley service install`` writes ``~/.config/systemd/user/bagley.service``. It never runs
systemctl or sudo itself: it prints the commands. ``--runner`` adds the steps for an always-on
laptop or tablet (a Surface): lingering, so the service runs with nobody logged in, and a logind
drop-in so closing the lid or leaving it idle doesn't suspend it.

The env file, ``~/.config/bagley/env`` (mode 0600), holds ``BAGLEY_*`` settings. The service
reads it through ``EnvironmentFile=`` and ``bagley`` loads it after a ``.env`` in the current
directory, so both see the same settings.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

UNIT_NAME = "bagley.service"
LOGIND_DROPIN = "/etc/systemd/logind.conf.d/bagley-runner.conf"
LOGIND_SETTINGS = {
    "HandleLidSwitch": "ignore",
    "HandleLidSwitchExternalPower": "ignore",
    "IdleAction": "ignore",
}
LINGER_DIR = Path("/var/lib/systemd/linger")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@,+=%~-]*$")
SECRET_KEY = re.compile(r"TOKEN|KEY|SECRET|PASS|AUTH|CREDENTIAL", re.I)
MAX_OUTPUT = 2_000_000

# Runs a program and returns (exit code, output). Tests pass a fake.
Run = Callable[[list[str]], tuple[int, str]]


def run_command(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run ``args`` (never through a shell) and return its exit code and output."""
    if not shutil.which(args[0]):
        return 127, f"{args[0]} is not installed."
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} did not answer within {timeout:.0f} s."
    except OSError as exc:
        return 126, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr or "")[:MAX_OUTPUT]


def systemd_available(platform: str = sys.platform) -> bool:
    return platform.startswith("linux") and shutil.which("systemctl") is not None


# Paths ------------------------------------------------------------------------------------------


def config_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config"


def unit_path(home: Path | None = None) -> Path:
    return config_dir(home) / "systemd" / "user" / UNIT_NAME


def env_path(home: Path | None = None) -> Path:
    return config_dir(home) / "bagley" / "env"


def logind_copy_path(home: Path | None = None) -> Path:
    """Where ``install --runner`` leaves the logind drop-in for the user to copy with sudo."""
    return config_dir(home) / "bagley" / "logind-runner.conf"


def source_root() -> str:
    """The folder to put on ``PYTHONPATH`` when Bagley runs from a checkout pip doesn't know
    about (the service starts in the home folder, where ``-m bagley`` wouldn't find it)."""
    try:
        importlib.metadata.distribution("bagley-assistant")
        return ""
    except importlib.metadata.PackageNotFoundError:
        return str(Path(__file__).resolve().parent.parent)


# Unit files -------------------------------------------------------------------------------------


def _quote(arg: str, *, exec_line: bool = True) -> str:
    """One word of an ``ExecStart=`` or ``Environment=`` line: specifiers and variables escaped,
    quoted when it has spaces."""
    arg = arg.replace("%", "%%")
    if exec_line:
        arg = arg.replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,-]+", arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def exec_args(python: str, host: str, port: int) -> list[str]:
    return [python, "-m", "bagley", "serve", "--no-browser", "--host", host, "--port", str(port)]


def render_unit(
    *, python: str, host: str, port: int, runner: bool = False, pythonpath: str = ""
) -> str:
    lines = [
        f"# Written by `bagley service install{' --runner' if runner else ''}`.",
        "# Settings go in ~/.config/bagley/env (`bagley env set KEY=VALUE`).",
        "[Unit]",
        "Description=Bagley assistant" + (" (always-on runner)" if runner else ""),
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=%h",
        "EnvironmentFile=-%h/.config/bagley/env",
    ]
    if pythonpath:
        lines.append("Environment=" + _quote(f"PYTHONPATH={pythonpath}", exec_line=False))
    lines += [
        "ExecStart=" + " ".join(_quote(a) for a in exec_args(python, host, port)),
        "Restart=on-failure",
        "RestartSec=3",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"


def render_logind() -> str:
    body = "\n".join(f"{k}={v}" for k, v in LOGIND_SETTINGS.items())
    return (
        "# Keep an always-on Bagley runner awake with the lid closed and while idle.\n"
        f"# Install as {LOGIND_DROPIN}; it applies after a reboot.\n"
        f"[Login]\n{body}\n"
    )


def install(
    *,
    python: str,
    host: str,
    port: int,
    runner: bool = False,
    pythonpath: str = "",
    home: Path | None = None,
) -> list[Path]:
    """Write the unit and, for a runner, a copy of the logind drop-in. Returns what was written."""
    unit = unit_path(home)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        render_unit(python=python, host=host, port=port, runner=runner, pythonpath=pythonpath),
        encoding="utf-8",
    )
    written = [unit]
    if runner:
        dropin = logind_copy_path(home)
        dropin.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        dropin.write_text(render_logind(), encoding="utf-8")
        written.append(dropin)
    return written


def uninstall(home: Path | None = None) -> list[Path]:
    """Remove the unit and its ``enable`` link. Returns what was removed."""
    unit = unit_path(home)
    removed = []
    for path in (unit.parent / "default.target.wants" / UNIT_NAME, unit):
        if path.is_symlink() or path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def status(
    run: Run = run_command,
    *,
    home: Path | None = None,
    user: str | None = None,
    linger_dir: Path = LINGER_DIR,
) -> dict[str, object]:
    code, out = run(["systemctl", "--user", "is-active", "bagley"])
    state = (out.strip().splitlines() or ["unknown"])[0]
    if code == 127:
        state = "no systemctl"
    _, enabled = run(["systemctl", "--user", "is-enabled", "bagley"])
    if user is None:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return {
        "unit": str(unit_path(home)),
        "installed": unit_path(home).is_file(),
        "active": code == 0 and state == "active",
        "state": state,
        "enabled": (enabled.strip().splitlines() or ["unknown"])[0],
        "linger": bool(user) and (linger_dir / user).exists(),
    }


# Env file ---------------------------------------------------------------------------------------


def _parse(line: str) -> tuple[str, str] | None:
    """``KEY=VALUE`` the way ``config.load_dotenv`` reads it."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip().removeprefix("export ").strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or env_path()
    if not path.is_file():
        return {}
    pairs = (_parse(line) for line in path.read_text(encoding="utf-8").splitlines())
    return dict(p for p in pairs if p)


def format_line(key: str, value: str) -> str:
    """A line that both systemd's ``EnvironmentFile=`` and ``load_dotenv`` read back as is."""
    if not ENV_KEY.match(key):
        raise ValueError(f"'{key}' is not a valid variable name.")
    if any(c in value for c in "\r\n\0"):
        raise ValueError(f"{key}: values must be a single line.")
    if SAFE_VALUE.match(value):
        return f"{key}={value}"
    if "'" in value or "\\" in value:
        raise ValueError(f"{key}: values can't contain ' or \\.")
    return f"{key}='{value}'"


def set_env(changes: Mapping[str, str | None], path: Path | None = None) -> Path:
    """Set (or with None, remove) variables, keeping the rest of the file and its comments.
    The file is written with mode 0600: it holds tokens."""
    path = path or env_path()
    for key in changes:
        if not ENV_KEY.match(key):
            raise ValueError(f"'{key}' is not a valid variable name.")
    new = {k: format_line(k, v) for k, v in changes.items() if v is not None}
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out: list[str] = []
    for raw in lines:
        parsed = _parse(raw)
        if parsed and parsed[0] in changes:
            if parsed[0] in new:
                out.append(new.pop(parsed[0]))  # Replaced in place; later duplicates go.
            continue
        out.append(raw)
    out += new.values()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n" if out else "")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)  # O_CREAT keeps the mode of a leftover file.
    os.replace(tmp, path)
    return path


def is_secret(key: str) -> bool:
    return bool(SECRET_KEY.search(key))


def masked(key: str, value: str) -> str:
    """The value as ``bagley env show`` prints it: secrets show only their last characters."""
    if not value or not is_secret(key):
        return value
    return "****" + value[-4:] if len(value) >= 12 else "****"
