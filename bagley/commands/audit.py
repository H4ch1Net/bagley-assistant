"""``bagley audit``: every tool call Bagley made, who allowed it and how it went.

Reads the running server (``GET /api/audit``) or, when none runs, the database directly. Lines
read like a ctOS terminal log, oldest first:

    071026-1643:07  EXEC  run_command   ASK>APPROVED  OK    412ms  BWRAP  {"command": "ls -la"}
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from bagley.client import Client, ServerUnavailable
from bagley.config import ServerConfig
from bagley.store import Store

POLL_SECONDS = 2.0
MAX_ROWS = 2000  # The most the server returns at once.


class Paint:
    """ctOS colours in 24-bit ANSI; plain text with ``NO_COLOR`` or when not on a terminal."""

    GRAY = (122, 122, 122)
    BODY = (202, 202, 202)
    WHITE = (255, 255, 255)
    OK = (0, 250, 154)
    ERROR = (252, 62, 56)

    def __init__(self, stream: TextIO | None = None, *, on: bool | None = None) -> None:
        stream = stream or sys.stdout
        if on is None:
            on = _isatty(stream) and not os.environ.get("NO_COLOR")
        self.on = on

    def rgb(self, color: tuple[int, int, int], text: str) -> str:
        if not self.on or not text:
            return text
        r, g, b = color
        return f"\033[38;2;{r};{g};{b}m{text}\033[0m"

    def gray(self, text: str) -> str:
        return self.rgb(self.GRAY, text)

    def body(self, text: str) -> str:
        return self.rgb(self.BODY, text)

    def white(self, text: str) -> str:
        return self.rgb(self.WHITE, text)

    def ok(self, text: str) -> str:
        return self.rgb(self.OK, text)

    def bad(self, text: str) -> str:
        return self.rgb(self.ERROR, text)


def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def terminal_width(stream: TextIO | None = None) -> int | None:
    """Columns of the terminal, or ``None`` when output goes to a file or pipe."""
    if not _isatty(stream or sys.stdout):
        return None
    return shutil.get_terminal_size((120, 24)).columns


def stamp(ts: float) -> str:
    """ctOS date and time: ``ddMMyy-hhmm:ss``."""
    return time.strftime("%d%m%y-%H%M:%S", time.localtime(ts))


def duration(ms: int | None) -> str:
    if ms is None:
        return ""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 100_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 1000}s"


def decision(entry: dict[str, Any]) -> str:
    """``ASK>APPROVED``, or ``AUTO`` for a tool that ran without asking."""
    permission = str(entry.get("permission") or "").upper()
    outcome = str(entry.get("decision") or "").upper()
    if outcome == "AUTO" and permission == "ALLOW":
        return "AUTO"
    if outcome in ("", "UNKNOWN"):
        return permission or "--"
    return f"{permission}>{outcome}" if permission else outcome


def format_entry(entry: dict[str, Any], paint: Paint, width: int | None = None) -> str:
    """One audit row as a terminal line, arguments cut to ``width`` columns when given."""
    ok = entry.get("ok")
    result = "--" if ok is None else ("OK" if ok else "FAIL")
    tags = " ".join(
        t
        for t in (
            "BWRAP" if entry.get("sandboxed") else "",
            "" if entry.get("source") in (None, "", "web") else str(entry["source"]),
        )
        if t
    )
    args = json.dumps(entry.get("arguments") or {}, ensure_ascii=False)
    columns = [
        (stamp(entry["created_at"]), 16, paint.gray),
        ("EXEC", 6, paint.body),
        (str(entry.get("tool", "")), 14, paint.white),
        (decision(entry), 14, paint.body),
        (result, 6, paint.ok if ok else (paint.bad if ok is False else paint.gray)),
        (duration(entry.get("duration_ms")), 7, paint.body),
        (tags, 7, paint.gray),
    ]
    used = 0
    parts = []
    for text, size, color in columns:
        cell = text.ljust(size - 1) + " " if len(text) < size else text + " "
        used += len(cell)
        parts.append(color(cell))
    if width is not None and used + len(args) > width:
        room = max(8, width - used)
        args = args[: room - 1] + "…" if len(args) > room else args
    return "".join(parts) + paint.body(args)


class AuditLog:
    """The audit log from the running server, or from the database when none runs."""

    def __init__(self, client: Client | None = None, store: Store | None = None) -> None:
        self.client = client
        self.store = store

    @classmethod
    def open(cls) -> AuditLog:
        client = Client()
        if client.available():
            return cls(client=client)
        client.close()
        path = ServerConfig.from_env().db_path
        return cls(store=Store(path) if Path(path).is_file() else None)

    @property
    def where(self) -> str:
        if self.client:
            return self.client.url
        return self.store.path if self.store else "no database"

    def fetch(
        self, limit: int, *, after: int = 0, tool: str = "", conversation: str = ""
    ) -> list[dict[str, Any]]:
        """Entries oldest first: the newest ``limit`` after id ``after``."""
        if self.client:
            rows = self.client.get(
                "/api/audit", limit=limit, after=after, tool=tool, conversation=conversation
            )
        elif self.store:
            rows = self.store.list_audit(
                limit, after=after, tool=tool, conversation_id=conversation
            )
        else:
            rows = []
        return sorted(rows, key=lambda r: r["id"])

    def close(self) -> None:
        if self.client:
            self.client.close()
        if self.store:
            self.store.close()


def cmd_audit(args: argparse.Namespace) -> int:
    limit = max(1, min(args.limit, MAX_ROWS))
    filters = {"tool": args.tool or "", "conversation": args.conversation or ""}
    paint = Paint()
    log = AuditLog.open()

    def show(rows: list[dict[str, Any]]) -> None:
        width = terminal_width()
        for row in rows:
            print(
                json.dumps(row, ensure_ascii=False)
                if args.json
                else format_entry(row, paint, width)
            )
        sys.stdout.flush()

    try:
        rows = log.fetch(limit, **filters)
        if args.export:
            with open(args.export, "w", encoding="utf-8") as out:
                for row in rows:
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[OK] EXPORTED {len(rows)} ENTRIES -> {args.export}")
            return 0
        show(rows)
        if not rows and not args.follow and not args.json:
            print(paint.gray("-- NO ENTRIES --"))
        last = rows[-1]["id"] if rows else 0
        offline = False
        while args.follow:
            time.sleep(POLL_SECONDS)
            try:
                rows = log.fetch(MAX_ROWS, after=last, **filters)
            except ServerUnavailable:
                if not offline:
                    print(paint.bad("[WARN] NO SIGNAL"), file=sys.stderr, flush=True)
                offline = True
                continue
            offline = False
            show(rows)
            last = rows[-1]["id"] if rows else last
    except ServerUnavailable as exc:
        print(f"[CRIT] Bagley is not reachable: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[CRIT] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        log.close()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("audit", help="Tool calls Bagley made, who allowed them and how they went")
    p.add_argument("-n", "--limit", type=int, default=50, help="How many recent calls (max 2000)")
    p.add_argument("-f", "--follow", action="store_true", help="Keep printing new calls")
    p.add_argument("--tool", metavar="NAME", help="Only calls to this tool")
    p.add_argument("--conversation", metavar="ID", help="Only calls from this conversation")
    p.add_argument("--json", action="store_true", help="One JSON object per line")
    p.add_argument("--export", metavar="FILE.jsonl", help="Write the entries to a JSONL file")
    p.set_defaults(func=cmd_audit)
