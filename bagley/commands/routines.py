"""``bagley routine``: list, show, run and delete saved routines.

``bagley routine run NAME`` lists the steps and asks once (y/N) unless ``--yes``; the steps then
run without asking again, as in the web UI. Every command uses the running server when there is
one and this process otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar
from urllib.parse import quote

from bagley.client import Client, ServerUnavailable

T = TypeVar("T")

# The ctOS terminal palette.
COLORS = {
    "gray": (122, 122, 122),
    "body": (202, 202, 202),
    "white": (255, 255, 255),
    "ok": (0, 250, 154),
    "error": (252, 62, 56),
}


class Paint:
    """24-bit ctOS colours on a terminal; plain text for pipes and with NO_COLOR."""

    def __init__(self, stream: Any = None) -> None:
        stream = stream or sys.stdout
        self.on = stream.isatty() and not os.environ.get("NO_COLOR")

    def __call__(self, color: str, text: str) -> str:
        if not self.on:
            return text
        r, g, b = COLORS[color]
        return f"\033[38;2;{r};{g};{b}m{text}\033[0m"


def _local(work: Callable[[Any], Awaitable[T]], *, start: bool = False) -> T:
    """Run ``work(runtime)`` in this process when no server answers."""
    from bagley.config import ServerConfig
    from bagley.runtime import Runtime

    async def main() -> T:
        rt = Runtime(ServerConfig.from_env())
        try:
            if start:
                await rt.start()  # Connect MCP servers, whose tools a routine may use.
            return await work(rt)
        finally:
            await rt.aclose()

    return asyncio.run(main())


class Backend:
    """The running server, or this process."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.remote = client.available()

    def list(self) -> list[dict[str, Any]]:
        if self.remote:
            return self.client.get("/api/routines")

        async def work(rt: Any) -> list[dict[str, Any]]:
            from bagley import routines

            return [routines.describe(rt, r) for r in routines.list_routines(rt)]

        return _local(work)

    def get(self, key: str) -> dict[str, Any] | None:
        if self.remote:
            try:
                return self.client.get(f"/api/routines/{quote(key, safe='')}")
            except ServerUnavailable as exc:
                if str(exc).startswith("404"):
                    return None
                raise

        async def work(rt: Any) -> dict[str, Any] | None:
            from bagley import routines

            routine = routines.get_routine(rt, key)
            return routines.describe(rt, routine) if routine else None

        return _local(work)

    def delete(self, routine: dict[str, Any]) -> None:
        if self.remote:
            resp = self.client.http.delete(f"/api/routines/{routine['id']}")
            if resp.status_code >= 400:
                raise ServerUnavailable(f"{resp.status_code}: {resp.text[:200]}")
            return

        async def work(rt: Any) -> None:
            from bagley import routines

            routines.delete_routine(rt, routine["id"])

        _local(work)

    def run(self, routine: dict[str, Any], show: Callable[[dict[str, Any]], None]) -> None:
        """Run it, calling ``show`` with each event (``routine.step``, ``routine.end``...)."""
        if self.remote:
            events: Iterator[dict[str, Any]] = self.client.stream(
                f"/api/routines/{routine['id']}/run", {"source": "cli"}
            )
            for event in events:
                show(event)
            return

        async def work(rt: Any) -> None:
            from bagley import routines

            saved = routines.get_routine(rt, routine["id"])
            if saved is None:
                show({"type": "error", "message": "The routine no longer exists."})
                return

            async def emit(event: dict[str, Any]) -> None:
                show(event)

            try:
                result = await routines.run_routine(rt, saved, source="cli", emit=emit)
            except routines.RoutineError as exc:
                show({"type": "error", "message": str(exc)})
                return
            show({"type": "routine.end", **result})

        _local(work, start=True)


def _when(ts: float | None) -> str:
    return time.strftime("%d%m%y -%H%M-", time.localtime(ts)) if ts else "--N/A--"


def _status(paint: Paint, status: str | None) -> str:
    if not status:
        return paint("gray", "--N/A--")
    return paint("ok", "[OK]") if status == "ok" else paint("error", "[FAIL]")


def _print_steps(paint: Paint, routine: dict[str, Any]) -> None:
    for number, step in enumerate(routine["steps"], 1):
        args = json.dumps(step["arguments"], ensure_ascii=False)
        args = args if len(args) <= 70 else args[:69] + "…"
        title = step.get("summary") or step["tool"]
        mark = "" if step.get("available", True) else paint("error", " [DENY]")
        index = f"{number:02d}"
        call = f"{step['tool']} {args}"
        print(f"  {paint('gray', index)}  {paint('white', title)}{mark}  {paint('gray', call)}")


def cmd_list(args: argparse.Namespace, backend: Backend, paint: Paint) -> int:
    items = backend.list()
    if args.json:
        print(json.dumps(items))
        return 0
    print(paint("white", f"» ROUTINES [{len(items):02d}]"))
    if not items:
        print(
            paint(
                "gray", "  NO ROUTINES. Save one in the web UI, or ask Bagley to save what it did."
            )
        )
    for r in items:
        number = f"#{r['id']:02d}"
        name = f"{r['name']:<20}"
        steps = f"{len(r['steps']):02d} STEPS"
        status = _status(paint, r.get("last_status"))
        about = r.get("description") or ""
        print(
            f"  {paint('gray', number)}  {paint('white', name)}  {paint('body', steps)}  "
            f"{status}  {paint('gray', about)}"
        )
    return 0


def cmd_show(args: argparse.Namespace, backend: Backend, paint: Paint) -> int:
    routine = backend.get(args.name)
    if routine is None:
        print(paint("error", f"[FAIL] NO ROUTINE NAMED {args.name!r}"), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(routine))
        return 0
    builtin = paint("gray", "  BUILTIN") if routine.get("builtin") else ""
    print(paint("white", f"» ROUTINE // {routine['name'].upper()}  #{routine['id']:02d}") + builtin)
    if routine.get("description"):
        print(f"  {paint('body', routine['description'])}")
    print(
        f"  {paint('gray', 'LAST RUN')}  {_when(routine.get('last_run'))}  "
        f"{_status(paint, routine.get('last_status'))}"
    )
    _print_steps(paint, routine)
    return 0


def cmd_run(args: argparse.Namespace, backend: Backend, paint: Paint) -> int:
    routine = backend.get(args.name)
    if routine is None:
        print(paint("error", f"[FAIL] NO ROUTINE NAMED {args.name!r}"), file=sys.stderr)
        return 1
    total = len(routine["steps"])
    print(paint("white", f"» ROUTINE // {routine['name'].upper()}  {total:02d} STEPS"))
    _print_steps(paint, routine)
    if not args.yes:
        try:
            answer = input(f"  Run these {total} steps? They won't ask again. [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print(paint("gray", "  ABORTED"))
            return 1
    outcome = {"ok": False}

    def show(event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "routine.step":
            mark = paint("ok", "[OK]  ") if event["ok"] else paint("error", "[FAIL]")
            result = " ".join(str(event.get("result") or "").split())
            result = result if len(result) <= 100 else result[:99] + "…"
            number = f"{event['index'] + 1:02d}"
            title = event.get("summary") or event["tool"]
            print(f"  {mark} {paint('gray', number)}  {title}: {result}", flush=True)
        elif kind == "routine.end":
            outcome["ok"] = bool(event["ok"])
            done = sum(1 for s in event["steps"] if s["ok"])
            if event["ok"]:
                print(paint("ok", f"  [OK] ROUTINE COMPLETE {done:02d}/{event['total']:02d}"))
            else:
                halted = event["failed_at"] + 1
                print(paint("error", f"  [CRIT] HALTED AT STEP {halted:02d}/{event['total']:02d}"))
        elif kind == "error":
            print(paint("error", f"  [CRIT] {event.get('message', '')}"), file=sys.stderr)

    backend.run(routine, show)
    return 0 if outcome["ok"] else 1


def cmd_delete(args: argparse.Namespace, backend: Backend, paint: Paint) -> int:
    routine = backend.get(args.name)
    if routine is None:
        print(paint("error", f"[FAIL] NO ROUTINE NAMED {args.name!r}"), file=sys.stderr)
        return 1
    backend.delete(routine)
    print(paint("ok", f"  [OK] DELETED {routine['name']}"))
    return 0


COMMANDS = {"list": cmd_list, "show": cmd_show, "run": cmd_run, "delete": cmd_delete}


def cmd_routine(args: argparse.Namespace) -> int:
    if not args.action:
        args.action = "list"
        args.json = False
    for stream in (sys.stdout, sys.stderr):  # Results may hold any text; Windows consoles vary.
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    paint = Paint()
    with Client() as client:
        try:
            return COMMANDS[args.action](args, Backend(client), paint)
        except ServerUnavailable as exc:
            print(paint("error", f"[CRIT] NO SIGNAL: {exc}"), file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130


def register(sub: argparse._SubParsersAction) -> None:
    routine = sub.add_parser("routine", help="List, show, run or delete saved routines")
    actions = routine.add_subparsers(dest="action")
    listing = actions.add_parser("list", help="List saved routines")
    listing.add_argument("--json", action="store_true", help="Print JSON")
    show = actions.add_parser("show", help="Show a routine's steps")
    show.add_argument("name", help="Routine name or number")
    show.add_argument("--json", action="store_true", help="Print JSON")
    run = actions.add_parser("run", help="Run a routine (asks once for all its steps)")
    run.add_argument("name", help="Routine name or number")
    run.add_argument("-y", "--yes", action="store_true", help="Don't ask; run the steps")
    delete = actions.add_parser("delete", help="Delete a routine")
    delete.add_argument("name", help="Routine name or number")
    routine.set_defaults(func=cmd_routine)
