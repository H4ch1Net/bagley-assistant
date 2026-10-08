"""``bagley runner``: the always-on Bagley (a Surface) that runs automations while this machine
sleeps, and ``bagley export`` / ``bagley import``: automations, memories and routines as JSON.

    bagley runner status [--json]
    bagley runner set URL [--token TOKEN]     (TOKEN "-" reads it from stdin)
    bagley runner push --all | ID ...
    bagley runner pull
    bagley export [--parts automations,memories,routines] [-o FILE]
    bagley import FILE                         (FILE "-" reads stdin)

They act on the Bagley the CLI talks to (``BAGLEY_URL``, by default this machine), and on this
machine's data directly when no server runs here.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from bagley import runner
from bagley.client import Client, ServerUnavailable
from bagley.config import ServerConfig
from bagley.readout import Readout

if TYPE_CHECKING:
    from bagley.runtime import Runtime  # Imported when needed: every `bagley` run loads this.

MAX_IMPORT_BYTES = 20_000_000

Work = Callable[["Runtime"], Awaitable[Any]]


def via(
    method: str,
    path: str,
    body: Any = None,
    *,
    local: Work,
    params: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> Any:
    """Ask the running server, or do the same work in this process when none runs here. With
    ``BAGLEY_URL`` set, a server that doesn't answer is an error, not a reason to use local data.
    Raises ServerUnavailable, runner.RunnerError or runner.DataError."""
    with Client(timeout=timeout) as client:
        if client.available():
            if method == "GET":
                return client.get(path, **(params or {}))
            if method == "PUT":
                try:
                    resp = client.http.put(path, json=body)
                except httpx.HTTPError as exc:
                    raise ServerUnavailable(str(exc)) from exc
                if resp.status_code >= 400:
                    raise ServerUnavailable(f"{resp.status_code}: {resp.text[:200]}")
                return resp.json()
            return client.post(path, body)
        if os.environ.get("BAGLEY_URL"):
            raise ServerUnavailable(f"NO SIGNAL from {client.url} (check BAGLEY_URL, BAGLEY_TOKEN)")
    return asyncio.run(_in_process(local))


async def _in_process(work: Work) -> Any:
    from bagley.runtime import Runtime

    rt = Runtime(ServerConfig.from_env())
    try:
        return await work(rt)
    finally:
        await rt.aclose()


def run(command: Callable[[Readout], int]) -> int:
    """Run a command, turning the expected failures into one ``[CRIT]`` line."""
    out = Readout()
    try:
        return command(out)
    except ServerUnavailable as exc:
        out.row("LINK", str(exc), "CRIT")
    except runner.RunnerError as exc:
        out.row("RUNNER", exc.message, "CRIT")
    except runner.DataError as exc:
        out.row("DATA", str(exc), "CRIT")
    return 1


def _secret(value: str, prompt: str) -> str:
    if value != "-":
        return value
    return (getpass.getpass(prompt) if sys.stdin.isatty() else sys.stdin.readline()).strip()


# Runner -----------------------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    def command(out: Readout) -> int:
        info = via("GET", "/api/runner", local=runner.probe, timeout=20)
        if args.json:
            print(json.dumps(info))
            return 0 if info["reachable"] else 1
        out.row("RUNNER", info["url"] or "--N/A--")
        if not info["configured"]:
            out.row(
                "LINK",
                "NOT CONFIGURED: bagley runner set https://<runner>.<tailnet>.ts.net",
                "WARN",
            )
            return 1
        if not info["reachable"]:
            out.row("LINK", f"NO SIGNAL  {info['error']}", "CRIT")
            return 1
        out.row("LINK", f"ONLINE  {info['latency_ms']} MS", "OK")
        out.row("VERSION", str(info["version"]))
        out.row("AUTOMATIONS", str(info["automations"]))
        return 0

    return run(command)


def cmd_set(args: argparse.Namespace) -> int:
    def command(out: Readout) -> int:
        changes = {"runner_url": args.url.strip().rstrip("/")}
        parts = urlsplit(changes["runner_url"])
        if changes["runner_url"] and (parts.scheme not in ("http", "https") or not parts.hostname):
            out.row("RUNNER", "USE A URL LIKE https://surface.tail1234.ts.net", "CRIT")
            return 2
        if args.token is not None:
            changes["runner_token"] = _secret(args.token, "Runner token: ")

        async def local(rt: Runtime) -> Any:
            rt.update_preferences(changes)

        try:
            via("PUT", "/api/preferences", changes, local=local)
        except ValueError as exc:
            out.row("RUNNER", str(exc), "CRIT")
            return 2
        out.row("RUNNER", changes["runner_url"] or "CLEARED", "OK")
        if "runner_token" in changes:
            out.row("TOKEN", "SET" if changes["runner_token"] else "CLEARED", "OK")
        return 0

    return run(command)


def cmd_push(args: argparse.Namespace) -> int:
    if not args.all and not args.ids:
        print("Pass --all or the automation ids (bagley runner push 3 5).", file=sys.stderr)
        return 2

    def command(out: Readout) -> int:
        body = {"all": True} if args.all else {"ids": args.ids}
        result = via(
            "POST",
            "/api/runner/push",
            body,
            local=lambda rt: runner.push(rt, None if args.all else args.ids),
        )
        for item in result["moved"]:
            out.row(f"#{item['id']}", f"{item['name']}  >> RUNNER #{item['runner_id']}", "OK")
        for item in result["failed"]:
            out.row(f"#{item['id']}", f"{item.get('name', '')}  {item['error']}", "CRIT")
        if not result["moved"] and not result["failed"]:
            out.row("PUSH", "NOTHING ENABLED TO MOVE", "WARN")
        return 0 if result["ok"] else 1

    return run(command)


def _counts(out: Readout, result: dict[str, Any]) -> None:
    out.print(out.paint("gray", f"{'PART':<12} {'ADDED':>6} {'SKIPPED':>8}"))
    for part in runner.PARTS:
        out.print(f"{part.upper():<12} {result['added'][part]:>6} {result['skipped'][part]:>8}")
    for error in result["errors"]:
        out.row(error["part"], f"{error['name']}  {error['error']}".strip(), "WARN")


def cmd_pull(args: argparse.Namespace) -> int:
    def command(out: Readout) -> int:
        result = via("POST", "/api/runner/pull", local=runner.pull)
        out.row("PULLED", result["runner"], "OK")
        out.note("  Automations arrive disabled, for reference.")
        _counts(out, result)
        return 0

    return run(command)


# Export and import ------------------------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> int:
    names = [p.strip() for p in args.parts.split(",") if p.strip()]

    async def local(rt: Runtime) -> Any:
        return runner.export_data(rt, names)

    def command(out: Readout) -> int:
        data = via("GET", "/api/export", local=local, params={"parts": ",".join(names)})
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            Readout(sys.stderr).row("EXPORTED", args.output, "OK")
        else:
            sys.stdout.write(text)
        return 0

    return run(command)


def cmd_import(args: argparse.Namespace) -> int:
    def command(out: Readout) -> int:
        if args.file == "-":
            raw = sys.stdin.read(MAX_IMPORT_BYTES + 1)
        else:
            path = Path(args.file)
            if path.stat().st_size > MAX_IMPORT_BYTES:
                raise runner.DataError("That file is too large.")
            raw = path.read_text(encoding="utf-8")
        if len(raw) > MAX_IMPORT_BYTES:
            raise runner.DataError("That export is too large.")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise runner.DataError(f"Not JSON: {exc}") from exc

        async def local(rt: Runtime) -> Any:
            return runner.import_data(rt, data)

        result = via("POST", "/api/import", data, local=local)
        _counts(out, result)
        return 0

    try:
        return run(command)
    except OSError as exc:
        Readout().row("FILE", str(exc), "CRIT")
        return 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("runner", help="The always-on Bagley that runs automations")
    actions = p.add_subparsers(dest="runner_command", required=True)
    status = actions.add_parser("status", help="Is the runner reachable?")
    status.add_argument("--json", action="store_true", help="As JSON")
    status.set_defaults(func=cmd_status)
    put = actions.add_parser("set", help="Set the runner URL (and token)")
    put.add_argument("url", help="e.g. https://surface.tail1234.ts.net ('' clears it)")
    put.add_argument("--token", help="The runner's BAGLEY_TOKEN ('-' reads it from stdin)")
    put.set_defaults(func=cmd_set)
    push = actions.add_parser("push", help="Move automations to the runner")
    push.add_argument("ids", nargs="*", type=int, metavar="ID", help="Automation ids")
    push.add_argument("--all", action="store_true", help="Every enabled automation")
    push.set_defaults(func=cmd_push)
    pull = actions.add_parser("pull", help="Copy the runner's automations here, disabled")
    pull.set_defaults(func=cmd_pull)

    export = sub.add_parser("export", help="Export automations, memories and routines as JSON")
    export.add_argument("--parts", default=",".join(runner.PARTS), help="Comma separated parts")
    export.add_argument("-o", "--output", help="Write to a file instead of stdout")
    export.set_defaults(func=cmd_export)
    imp = sub.add_parser("import", help="Merge an export into this Bagley")
    imp.add_argument("file", help="An export file, or - for stdin")
    imp.set_defaults(func=cmd_import)
