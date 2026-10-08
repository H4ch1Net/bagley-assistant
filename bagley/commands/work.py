"""``bagley assets``, ``bagley health`` and ``bagley ticket``: the inventory, client health
checks and ticket summaries from the command line.

The commands talk to a running Bagley when one answers, and otherwise do the work in this
process against the same database, so they work with or without the server running.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from bagley.cli import Style
from bagley.client import Client, ServerUnavailable

STATUS_LIMIT = {"OK": 0, "UNKNOWN": 0, "WARN": 1, "CRIT": 2}
MAX_NOTES_STDIN = 50_000


def _err(message: str) -> int:
    print(Style(sys.stderr).bad(message), file=sys.stderr)
    return 1


def _online() -> Client | None:
    client = Client()
    try:
        return client if client.available() else None
    except ServerUnavailable:
        return None


def _in_process(coro_factory: Any) -> Any:
    """Run ``coro_factory(rt)`` against an in-process runtime built from the environment."""
    from bagley.config import ServerConfig
    from bagley.runtime import Runtime

    async def main() -> Any:
        rt = Runtime(ServerConfig.from_env())
        await rt.start()
        try:
            return await coro_factory(rt)
        finally:
            await rt.aclose()

    return asyncio.run(main())


# assets -----------------------------------------------------------------------------------------


def _status_cell(style: Style, status: str) -> str:
    text = f"[{status}]".ljust(9)
    if status == "CRIT":
        return style.bad(text)
    if status == "WARN":
        return style.warn(text)
    if status == "OK":
        return style.ok(text)
    return style.dim(text)


def _print_assets(assets: list[dict[str, Any]], style: Style) -> None:
    if not assets:
        print(style.dim("NO ASSETS"))
        return
    width = max((len(a["name"]) for a in assets), default=4)
    for asset in assets:
        status = asset.get("status") or "UNCHECKED"
        where = asset.get("hostname") or asset.get("ip") or "--"
        line = (
            f"{_status_cell(style, status)} "
            f"{asset['client']:<16.16} {asset['name']:<{width}.{width}} "
            f"{asset['kind']:<8} {where}"
        )
        print(line.rstrip())


def _assets_from(args: argparse.Namespace, client: Client | None) -> list[dict[str, Any]]:
    if client is not None:
        return client.get("/api/assets", client=args.client or "").get("assets", [])
    from bagley import work

    return _in_process(lambda rt: _as_coro(work.list_assets(rt.store, args.client or None)))


async def _as_coro(value: Any) -> Any:
    return value


def cmd_assets(args: argparse.Namespace) -> int:
    style = Style()
    action = args.action or "list"
    client = _online()
    from bagley import work

    try:
        if action == "list":
            assets = _assets_from(args, client)
            _print_assets(assets, style)
            return 0

        if action == "export":
            if client is not None:
                csv_text = client.http.get(
                    "/api/assets/export", params={"client": args.client or ""}
                ).text
            else:
                csv_text = _in_process(
                    lambda rt: _as_coro(work.export_csv(rt.store, args.client or None))
                )
            sys.stdout.write(csv_text)
            return 0

        if action == "import":
            if not args.file:
                return _err("Usage: bagley assets import FILE [--client X]")
            data = Path(args.file).read_bytes()
            text = work.decode_csv(data)
            if client is not None:
                result = client.http.post(
                    "/api/assets/import",
                    params={"client": args.client or ""},
                    content=text.encode("utf-8"),
                    headers={"Content-Type": "text/csv"},
                ).json()
            else:
                result = _in_process(
                    lambda rt: _as_coro(work.import_csv(rt.store, text, args.client or None))
                )
            print(
                f"[OK] +{result['added']} added, {result['updated']} updated, "
                f"{result['skipped']} skipped"
            )
            for bad in result.get("errors", [])[:10]:
                print(style.warn(f"  line {bad['line']}: {bad['error']}"))
            if result.get("ignored_columns"):
                print(style.dim("  ignored columns: " + ", ".join(result["ignored_columns"])))
            return 0

        if action == "add":
            return _add_asset(args, client, style)
    except FileNotFoundError:
        return _err(f"No such file: {args.file}")
    except work.AssetError as exc:
        return _err(str(exc))
    except ServerUnavailable as exc:
        return _err(f"Bagley is not reachable: {exc}")
    return _err(f"Unknown action '{action}'. Use list, add, import or export.")


def _add_asset(args: argparse.Namespace, client: Client | None, style: Style) -> int:
    from bagley import work

    fields = dict(args.field or [])
    if args.client:
        fields.setdefault("client", args.client)
    for key in ("client", "name"):
        if not fields.get(key):
            return _err(f"An asset needs a {key}. Add it with {key}=... (or --client).")
    if client is not None:
        body = {"client": fields.pop("client"), "name": fields.pop("name"), **fields}
        asset = client.post("/api/assets", body)
    else:
        asset = _in_process(lambda rt: _as_coro(work.add_asset(rt.store, fields)))
    print(f"[OK] added asset #{asset['id']}: {asset['client']} / {asset['name']}")
    return 0


# health -----------------------------------------------------------------------------------------


def cmd_health(args: argparse.Namespace) -> int:
    style = Style()
    client = _online()
    try:
        if client is not None:
            result = client.post("/api/work/health", {"client": args.client or None})
        else:
            from bagley import work

            result = _in_process(lambda rt: work.check_assets(rt, args.client or None))
    except ServerUnavailable as exc:
        return _err(f"Bagley is not reachable: {exc}")
    if args.json:
        print(json.dumps(result, default=str))
    else:
        summary = result["summary"]
        if not result["assets"] and not result["findings"]:
            print(style.dim("NO ASSETS WITH CHECKS"))
            return 0
        for line in result["report"].splitlines():
            if "CRIT" in line:
                print(style.bad(line))
            elif "WARN" in line:
                print(style.warn(line))
            else:
                print(line)
        head = " ".join(
            f"{summary[s]} {s}" for s in ("OK", "WARN", "CRIT", "UNKNOWN") if summary[s]
        )
        print(style.dim(f"// {head or 'NO CHECKS'}"))
    return 2 if result["summary"]["CRIT"] else 0


# ticket -----------------------------------------------------------------------------------------


def _read_notes() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.read(MAX_NOTES_STDIN + 1)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".ticket.txt", delete=False) as handle:
        handle.write("# Paste the ticket notes below this line, then save and close.\n")
        path = handle.name
    try:
        subprocess.run([*editor.split(), path], check=False)
        text = Path(path).read_text("utf-8", "replace")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("# Paste the ticket"))


def cmd_ticket(args: argparse.Namespace) -> int:
    style = Style()
    notes = _read_notes().strip()
    if not notes:
        return _err("No ticket notes given (pipe them in or write them in the editor).")
    body = {"notes": notes[:MAX_NOTES_STDIN], "client": args.client or None, "languages": args.lang}
    client = _online()
    try:
        if client is not None:
            result = client.post("/api/work/ticket-summary", body)
        else:
            from bagley import work

            result = _in_process(
                lambda rt: work.ticket_summary(rt, notes, args.client or None, args.lang or None)
            )
    except ServerUnavailable as exc:
        return _err(f"Bagley is not reachable: {exc}")
    except ValueError as exc:
        return _err(str(exc))
    print(result["markdown"])
    if result.get("machine"):
        print(style.dim(f"\n// {' + '.join(result['languages'])} via {result['machine']}"))
    return 0


# Registration -----------------------------------------------------------------------------------


def _field(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("fields are key=value, e.g. ip=10.0.0.5")
    key, val = value.split("=", 1)
    return key.strip(), val.strip()


def register(sub: argparse._SubParsersAction) -> None:
    assets = sub.add_parser("assets", help="Client asset inventory")
    assets.add_argument(
        "action", nargs="?", default="list", choices=["list", "add", "import", "export"]
    )
    assets.add_argument("file", nargs="?", help="CSV file for import")
    assets.add_argument("--client", default="", help="Limit to or set the client")
    assets.add_argument(
        "--field", action="append", type=_field, metavar="key=value", help="Field for add"
    )
    assets.set_defaults(func=cmd_assets)

    health = sub.add_parser("health", help="Run client health checks (exit 2 on CRIT)")
    health.add_argument("--client", default="", help="Only this client")
    health.add_argument("--json", action="store_true", help="Print the full result as JSON")
    health.set_defaults(func=cmd_health)

    ticket = sub.add_parser("ticket", help="Summarise ticket notes for a client")
    ticket.add_argument("--client", default="", help="The client the ticket is for")
    ticket.add_argument(
        "--lang", action="append", help="A language for the summary (repeat for more)"
    )
    ticket.set_defaults(func=cmd_ticket)
