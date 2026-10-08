"""``bagley service``: run Bagley in the background with systemd, and ``bagley env``: the settings
file the service reads.

    bagley service install [--runner] [--host H] [--port P] [--print]
    bagley service uninstall
    bagley service status [--json]
    bagley env set KEY=VALUE ...   (VALUE "-" reads it from stdin)
    bagley env unset KEY ...
    bagley env show [--json]

Nothing here runs systemctl or sudo to change the system: the commands are printed for the user.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import shlex
import stat
import sys
from pathlib import Path

from bagley import service
from bagley.config import LOOPBACK_HOSTS, ServerConfig
from bagley.readout import Readout


def _manual(out: Readout, python: str, platform: str) -> None:
    """How to start Bagley at login where there is no systemd."""
    out.row("SERVICE", "ONLY SYSTEMD (LINUX) IS SUPPORTED", "WARN")
    out.print()
    out.head("Run Bagley at login by hand")
    command = f"{shlex.quote(python)} -m bagley serve --no-browser"
    if platform == "darwin":
        out.note("  System Settings > General > Login Items: add a script that runs")
        out.cmd(command)
        out.note("  or a LaunchAgent in ~/Library/LaunchAgents with that command and KeepAlive.")
    elif platform == "win32":
        pythonw = Path(python).with_name("pythonw.exe")  # No console window.
        out.note("  Task Scheduler, at log on:")
        out.cmd(
            f'schtasks /Create /SC ONLOGON /TN Bagley /TR "\\"{pythonw}\\" -m bagley serve --no-browser"'
        )
    else:
        out.note("  Add this to your session's autostart:")
        out.cmd(command)
    out.note("  Settings for it go in ~/.config/bagley/env (`bagley env set KEY=VALUE`).")


def cmd_install(args: argparse.Namespace) -> int:
    out = Readout()
    config = ServerConfig.from_env()
    host = getattr(args, "host", None) or config.host
    port = getattr(args, "port", None) or config.port
    python = sys.executable
    pythonpath = service.source_root()
    if args.print:
        sys.stdout.write(
            service.render_unit(
                python=python, host=host, port=port, runner=args.runner, pythonpath=pythonpath
            )
        )
        return 0
    if not service.systemd_available():
        _manual(out, python, sys.platform)
        return 1
    existed = service.unit_path().is_file()
    written = service.install(
        python=python, host=host, port=port, runner=args.runner, pythonpath=pythonpath
    )
    out.row("UNIT", str(written[0]), "OK")
    out.row("EXEC", " ".join(service.exec_args(python, host, port)))
    if pythonpath:
        out.row("PYTHONPATH", pythonpath)
    env_file = service.env_path()
    out.row("ENV FILE", str(env_file) + ("" if env_file.is_file() else "  (none yet)"))
    if host not in LOOPBACK_HOSTS and not os.environ.get("BAGLEY_TOKEN"):
        out.row("TOKEN", "BEYOND LOCALHOST WITHOUT A FIXED TOKEN: IT CHANGES EVERY START", "WARN")
        out.cmd(f"bagley env set BAGLEY_TOKEN={secrets.token_urlsafe(24)}")
    out.print()
    out.head("Start it")
    out.cmd("systemctl --user daemon-reload")
    out.cmd("systemctl --user enable --now bagley")
    if existed:
        out.cmd("systemctl --user restart bagley")
    out.note("  Logs: journalctl --user -u bagley -f")
    if args.runner:
        out.print()
        out.head("Always-on runner")
        out.note("  Keep running with nobody logged in:")
        out.cmd("sudo loginctl enable-linger $USER")
        out.note("  Ignore the lid and idle (logind drop-in, applies after a reboot):")
        out.cmd(f"sudo install -Dm644 {shlex.quote(str(written[1]))} {service.LOGIND_DROPIN}")
        out.note("  Then reach it from the phone and the laptop: bagley tailscale")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    out = Readout()
    removed = service.uninstall()
    if not removed:
        out.row("UNIT", "NOT INSTALLED", "WARN")
    for path in removed:
        out.row("REMOVED", str(path), "OK")
    out.print()
    out.head("Stop it")
    out.cmd("systemctl --user stop bagley")
    out.cmd("systemctl --user daemon-reload")
    out.note("  If it was a runner, also undo:")
    out.cmd("sudo loginctl disable-linger $USER")
    out.cmd(f"sudo rm {service.LOGIND_DROPIN}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    out = Readout()
    if not service.systemd_available():
        if args.json:
            print(json.dumps({"supported": False}))
        else:
            _manual(out, sys.executable, sys.platform)
        return 1
    info = service.status(service.run_command)
    if args.json:
        print(json.dumps({"supported": True, **info}))
        return 0 if info["active"] else 1
    out.row("SERVICE", service.UNIT_NAME)
    out.row("UNIT", str(info["unit"]) if info["installed"] else "NOT INSTALLED")
    out.row("STATE", str(info["state"]).upper(), "OK" if info["active"] else "CRIT")
    out.row("ENABLED", str(info["enabled"]).upper())
    if info["linger"]:
        out.row("LINGER", "ON", "OK")
    else:
        out.row("LINGER", "OFF  STOPS AT LOGOUT (sudo loginctl enable-linger $USER)", "WARN")
    return 0 if info["active"] else 1


# Env file ---------------------------------------------------------------------------------------


def cmd_env_set(args: argparse.Namespace) -> int:
    out = Readout()
    changes: dict[str, str | None] = {}
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            print(f"Use KEY=VALUE, got '{pair}'.", file=sys.stderr)
            return 2
        if value == "-":
            value = (
                getpass.getpass(f"{key}: ") if sys.stdin.isatty() else sys.stdin.readline()
            ).strip()
        changes[key.strip()] = value
    return _save(out, changes)


def cmd_env_unset(args: argparse.Namespace) -> int:
    return _save(Readout(), dict.fromkeys(args.keys))


def _save(out: Readout, changes: dict[str, str | None]) -> int:
    try:
        path = service.set_env(changes)
    except ValueError as exc:
        print(f"[CRIT] {exc}", file=sys.stderr)
        return 2
    width = max(12, *(len(k) for k in changes))
    for key, value in changes.items():
        out.row(key, "REMOVED" if value is None else service.masked(key, value), "OK", width)
    out.note(f"  Saved to {path}. Restart Bagley to apply: systemctl --user restart bagley")
    return 0


def cmd_env_show(args: argparse.Namespace) -> int:
    out = Readout()
    path = service.env_path()
    values = {k: service.masked(k, v) for k, v in service.read_env(path).items()}
    if args.json:
        print(json.dumps({"path": str(path), "values": values}))
        return 0
    out.row("ENV FILE", str(path) + ("" if path.is_file() else "  (none yet)"))
    if path.is_file() and os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        out.row("MODE", f"READABLE BY OTHERS: chmod 600 {shlex.quote(str(path))}", "WARN")
    width = max([12, *(len(k) for k in values)])
    for key, value in values.items():
        out.row(key, value, width=width)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    svc = sub.add_parser("service", help="Run Bagley in the background (systemd user service)")
    actions = svc.add_subparsers(dest="service_command", required=True)
    install = actions.add_parser("install", help="Write the systemd user unit")
    install.add_argument(
        "--runner",
        action="store_true",
        help="Always-on runner: also print the lingering and lid switch steps",
    )
    install.add_argument("--host", default=argparse.SUPPRESS, help="Bind address for the service")
    install.add_argument("--port", type=int, default=argparse.SUPPRESS, help="Port")
    install.add_argument("--print", action="store_true", help="Only print the unit")
    install.set_defaults(func=cmd_install)
    actions.add_parser("uninstall", help="Remove the unit").set_defaults(func=cmd_uninstall)
    status = actions.add_parser("status", help="Is the service running?")
    status.add_argument("--json", action="store_true", help="As JSON")
    status.set_defaults(func=cmd_status)

    env = sub.add_parser("env", help="Settings in ~/.config/bagley/env (read by the service)")
    env_actions = env.add_subparsers(dest="env_command", required=True)
    put = env_actions.add_parser("set", help="Set KEY=VALUE (VALUE '-' reads it from stdin)")
    put.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    put.set_defaults(func=cmd_env_set)
    unset = env_actions.add_parser("unset", help="Remove variables")
    unset.add_argument("keys", nargs="+", metavar="KEY")
    unset.set_defaults(func=cmd_env_unset)
    show = env_actions.add_parser("show", help="Show the file with secrets masked")
    show.add_argument("--json", action="store_true", help="As JSON")
    show.set_defaults(func=cmd_env_show)
