"""``bagley briefing`` and ``bagley watchdog``: the system and security report in the terminal.

The report is always collected in this process, so it describes the computer the command runs
on even when ``BAGLEY_URL`` points at a server elsewhere. Baselines live in the same database
as the server's, so both see the same known devices and ports.

``bagley briefing`` exits with 2 when anything is critical, 1 on a warning and 0 otherwise,
so scripts and status bars can act on it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys

from bagley.cli import Style
from bagley.config import ServerConfig, resolve_preferences
from bagley.store import Store
from bagley.watchdog.baseline import Baseline
from bagley.watchdog.collectors import COLLECTORS, Context
from bagley.watchdog.report import Report, collect, section_ids

EXIT = {"crit": 2, "warn": 1, "ok": 0}
COLOURS = {
    "ok": "#00FA9A",
    "crit": "#FC3E38",
    "warn": "#FFFFFF",
    "info": "#CACACA",
    "unavailable": "#7A7A7A",
    "label": "#7A7A7A",
    "body": "#CACACA",
    "title": "#FFFFFF",
}


class Paint(Style):
    """24-bit ctOS colours, off for pipes and with NO_COLOR."""

    def __call__(self, role: str, text: str) -> str:
        colour = COLOURS.get(role)
        if not colour or not self.on or not text:
            return text
        r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
        bold = "1;" if role in ("crit", "title") else ""
        return f"\033[{bold}38;2;{r};{g};{b}m{text}\033[0m"


def _sections(values: list[str] | None) -> list[str] | None:
    """Validated section ids, or None for all. Raises ValueError for unknown ones."""
    return section_ids(values) if values else None


def make_context(store: Store) -> Context:
    """The collectors' context for this process (tests replace it)."""
    return Context(baseline=Baseline(store))


async def _briefing(sections: list[str] | None, notify: bool) -> Report:
    import httpx

    from bagley import notify as notifications

    config = ServerConfig.from_env()
    store = Store(config.db_path)
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            report = await collect(None, sections, context=make_context(store))
            if notify:
                prefs, _ = resolve_preferences(store.get_preferences(), os.environ)
                base = os.environ.get("BAGLEY_PUBLIC_URL") or f"http://127.0.0.1:{config.port}"
                note = notifications.Note(
                    report.title_line(),
                    report.headline(),
                    level=report.level(),
                    url=base.rstrip("/") + "/",
                    tags=["warning"] if report.level() == "critical" else [],
                )
                await notifications.fan_out(prefs, http, note, desktop=True)
    finally:
        store.close()
    return report


def cmd_briefing(args: argparse.Namespace) -> int:
    try:
        sections = _sections(args.sections)
    except ValueError as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 64
    # Windows consoles and pipes default to a code page without "·" and "×".
    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    try:
        report = asyncio.run(_briefing(sections, args.notify))
    except KeyboardInterrupt:
        return 130
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    elif args.markdown:
        print(report.markdown())
    else:
        print(report.text(Paint(sys.stdout)))
    return EXIT[report.status()]


def cmd_baseline_reset(args: argparse.Namespace) -> int:
    try:
        sections = _sections(args.sections)
    except ValueError as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 64
    store = Store(ServerConfig.from_env().db_path)
    try:
        removed = Baseline(store).reset(sections)
    finally:
        store.close()
    names = [i for i in sections or COLLECTORS if COLLECTORS[i].baseline]
    paint = Paint(sys.stdout)
    print(
        paint("ok", "[OK]")
        + " "
        + paint("body", f"BASELINE CLEARED // {', '.join(names).upper() or 'NOTHING TO CLEAR'}")
        + paint("label", f"  {len(removed)} record(s); the next run records it again")
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    ids = ", ".join(COLLECTORS)
    brief = sub.add_parser(
        "briefing",
        help="System health and security report (exit 2 critical, 1 warning)",
        description=f"Check this computer. Sections: {ids}.",
    )
    brief.add_argument(
        "-s",
        "--sections",
        nargs="+",
        metavar="ID",
        help="Only these sections (space or comma separated)",
    )
    out = brief.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="Print the report as JSON")
    out.add_argument("--markdown", action="store_true", help="Print the report as Markdown")
    brief.add_argument(
        "--notify", action="store_true", help="Also send the headline to the desktop and phone"
    )
    brief.set_defaults(func=cmd_briefing)

    watchdog = sub.add_parser("watchdog", help="Watchdog baselines (known devices, ports, peers)")
    actions = watchdog.add_subparsers(dest="watchdog_command")
    reset = actions.add_parser(
        "baseline-reset", help="Forget known devices, ports and peers; the next run records them"
    )
    reset.add_argument("-s", "--sections", nargs="+", metavar="ID", help="Only these sections")
    reset.set_defaults(func=cmd_baseline_reset)
    watchdog.set_defaults(func=lambda args: watchdog.print_help() or 0)
