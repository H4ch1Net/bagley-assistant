"""``bagley desktop install`` and ``bagley desktop path``: put the desktop files in place.

    bagley desktop install [--only zsh,quickshell,hypr,mako,systemd] [--bar PATH]
                           [--edit-configs] [--dry-run]
    bagley desktop path

The files ship inside the package (``desktop/`` in the repository): the Quickshell overlay and
bar segment, the Hyprland binds, the mako style, the zsh plugin and the voice listener's systemd
unit. ``install`` copies them where each program looks and prints the one line to add to your
own config. With ``--edit-configs`` it adds those lines itself, once, between ``# >>> bagley``
markers, after saving a ``.bagley-bak`` copy of the file. It never needs root: when the ctOS bar
folder isn't writable it prints the ``sudo cp`` to run instead.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import bagley
from bagley.commands.audit import Paint

COMPONENTS = ("zsh", "quickshell", "hypr", "mako", "systemd")
BARS = ("/opt/ctos/bar.qml", "~/ctOS/bar.qml")
DEFAULT_IPC = "/opt/ctos/bar.qml"
MARK_START = "# >>> bagley >>>"
MARK_END = "# <<< bagley <<<"


def files_dir() -> Path:
    """Where the desktop files are: inside the installed package, else the repository."""
    root = Path(bagley.__file__).resolve().parent
    for candidate in (root / "desktop" / "files", root.parent / "desktop"):
        if (candidate / "quickshell").is_dir():
            return candidate
    raise FileNotFoundError("Bagley's desktop files are missing from this installation.")


@dataclass
class Report:
    done: list[str] = field(default_factory=list)  # "[OK] ..." lines
    todo: list[str] = field(default_factory=list)  # what the user still has to do


@dataclass
class Setup:
    home: Path
    src: Path
    dry_run: bool = False
    edit: bool = False
    env: dict[str, str] | os._Environ[str] = field(default_factory=lambda: os.environ)
    report: Report = field(default_factory=Report)

    def short(self, path: Path) -> str:
        try:
            return "~/" + str(path.relative_to(self.home))
        except ValueError:
            return str(path)

    def copy(self, source: Path, target: Path, replace: dict[str, str] | None = None) -> None:
        if not self.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif replace:
                text = source.read_text(encoding="utf-8")
                for old, new in replace.items():
                    text = text.replace(old, new)
                target.write_text(text, encoding="utf-8")
            else:
                shutil.copy2(source, target)
        self.report.done.append(f"{source.name} -> {self.short(target)}")

    def add_block(self, config: Path, block: str, line_hint: str) -> None:
        """Put ``block`` between the markers in ``config`` (or say to), replacing an old one."""
        if not self.edit:
            self.report.todo.append(f"Add to {self.short(config)}: {line_hint}")
            return
        old = config.read_text(encoding="utf-8") if config.exists() else ""
        start, end = old.find(MARK_START), old.find(MARK_END)
        wrapped = f"{MARK_START}\n{block.rstrip()}\n{MARK_END}\n"
        if start >= 0 and end > start:
            new = old[:start] + wrapped + old[end + len(MARK_END) :].lstrip("\n")
        else:
            new = old + ("\n" if old and not old.endswith("\n") else "") + wrapped
        if new == old:
            self.report.done.append(f"{self.short(config)} already set up")
            return
        if not self.dry_run:
            config.parent.mkdir(parents=True, exist_ok=True)
            backup = config.with_name(config.name + ".bagley-bak")
            if config.exists() and not backup.exists():
                shutil.copy2(config, backup)
            config.write_text(new, encoding="utf-8")
        self.report.done.append(f"edited {self.short(config)}")

    # Components ------------------------------------------------------------------------------

    def data_home(self) -> Path:
        xdg = self.env.get("XDG_DATA_HOME")
        return Path(xdg) if xdg else self.home / ".local" / "share"

    def config_home(self) -> Path:
        xdg = self.env.get("XDG_CONFIG_HOME")
        return Path(xdg) if xdg else self.home / ".config"

    def zsh(self) -> None:
        target = self.data_home() / "bagley" / "zsh"
        self.copy(self.src / "zsh", target)
        line = f"source {self.short(target / 'bagley.zsh')}"
        self.add_block(self.home / ".zshrc", line, line)

    def quickshell(self, bar: Path | None) -> str:
        """Install the QML; returns the path ``qs ipc -p`` must name."""
        source = self.src / "quickshell" / "bagley"
        if bar is None:
            target = self.config_home() / "quickshell" / "bagley"
            self.copy(source, target)
            shell = target / "shell.qml"
            self.report.todo.append(
                f"Start the overlay with Hyprland: qs -p {self.short(shell)} (no ctOS bar found; "
                "--bar PATH puts it in the bar)"
            )
            return str(shell)
        target = bar.parent / "bagley"
        if os.access(bar.parent, os.W_OK):
            self.copy(source, target)
        else:
            self.report.todo.append(f"Copy the QML (needs root): sudo cp -r {source} {bar.parent}/")
        self.report.todo.append(
            f"In {bar}: add `import qs.bagley`, put `BagleyOverlay {{}}` in the root PanelWindow "
            "and `BagleySegment {}` + `Divider {}` first in the right-hand Row, then restart the "
            "bar (docs/desktop.md)"
        )
        return str(bar)

    def hypr(self, ipc: str) -> None:
        hypr = self.config_home() / "hypr"
        replace = {DEFAULT_IPC: ipc} if ipc != DEFAULT_IPC else None
        if (hypr / "hyprland.lua").exists():
            self.copy(self.src / "hypr" / "bagley.lua", hypr / "bagley.lua", replace)
            self.add_block(hypr / "hyprland.lua", 'require("bagley")', 'require("bagley")')
        else:
            self.copy(self.src / "hypr" / "bagley.conf", hypr / "bagley.conf", replace)
            line = f"source = {self.short(hypr / 'bagley.conf')}"
            self.add_block(hypr / "hyprland.conf", line, line)

    def mako(self) -> None:
        mako = self.config_home() / "mako"
        source = self.src / "mako" / "bagley.ini"
        self.copy(source, mako / "bagley.ini")
        sections = source.read_text(encoding="utf-8")
        self.add_block(
            mako / "config", sections, f"the sections of {self.short(mako / 'bagley.ini')}"
        )
        self.report.todo.append("Reload mako: makoctl reload")

    def systemd(self) -> None:
        unit = self.config_home() / "systemd" / "user" / "bagley-listen.service"
        self.copy(self.src / "systemd" / "bagley-listen.service", unit)
        self.report.todo.append(
            "For the wake word: systemctl --user daemon-reload && "
            "systemctl --user enable --now bagley-listen"
        )


def find_bar(home: Path, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    for candidate in BARS:
        path = Path(candidate.replace("~", str(home), 1))
        if path.is_file():
            return path
    return None


def install(
    home: Path,
    *,
    only: list[str] | None = None,
    bar: str | None = None,
    edit: bool = False,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    src: Path | None = None,
) -> Report:
    setup = Setup(
        home=home, src=src or files_dir(), dry_run=dry_run, edit=edit, env=env or os.environ
    )
    chosen = only or list(COMPONENTS)
    ipc = DEFAULT_IPC
    if "zsh" in chosen:
        setup.zsh()
    if "quickshell" in chosen:
        ipc = setup.quickshell(find_bar(home, bar))
    elif bar:
        ipc = str(Path(bar).expanduser())
    if "hypr" in chosen:
        setup.hypr(ipc)
    if "mako" in chosen:
        setup.mako()
    if "systemd" in chosen:
        setup.systemd()
    return setup.report


def cmd_install(args: argparse.Namespace) -> int:
    only = [c.strip() for c in (args.only or "").split(",") if c.strip()] or None
    unknown = sorted(set(only or ()) - set(COMPONENTS))
    if unknown:
        print(f"[CRIT] Unknown part: {', '.join(unknown)}. Choose from {', '.join(COMPONENTS)}.", file=sys.stderr)  # fmt: skip
        return 2
    try:
        report = install(
            Path.home(), only=only, bar=args.bar, edit=args.edit_configs, dry_run=args.dry_run
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"[CRIT] {exc}", file=sys.stderr)
        return 1
    paint = Paint()
    prefix = "[DRY] " if args.dry_run else "[OK]  "
    for line in report.done:
        print(paint.ok(prefix) + line)
    for line in report.todo:
        print(paint.gray("[--]  ") + line)
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    try:
        print(files_dir())
    except FileNotFoundError as exc:
        print(f"[CRIT] {exc}", file=sys.stderr)
        return 1
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("desktop", help="Install the Quickshell overlay, Hyprland binds and more")
    p.set_defaults(func=cmd_path)
    actions = p.add_subparsers(dest="action")
    inst = actions.add_parser("install", help="Copy the desktop files where each program looks")
    inst.add_argument("--only", metavar="PARTS", help=f"Comma separated: {', '.join(COMPONENTS)}")
    inst.add_argument("--bar", metavar="PATH", help="The ctOS bar.qml (default: found)")
    inst.add_argument(
        "--edit-configs",
        action="store_true",
        help="Also add the lines to ~/.zshrc, hyprland and mako (with a backup)",
    )
    inst.add_argument("--dry-run", action="store_true", help="Show what would happen")
    inst.set_defaults(func=cmd_install)
    path = actions.add_parser("path", help="Print where the desktop files are")
    path.set_defaults(func=cmd_path)
