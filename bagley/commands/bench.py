"""``bagley bench``: test every installed model on tool-use tasks and pick the best per machine.

Uses the running server when there is one (``POST /api/bench/run``), otherwise runs in this
process. Progress is printed live, then one table per machine with the winner marked ``>``.
``--apply`` makes the winners the default models.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from bagley.client import ServerUnavailable
from bagley.commands.audit import Paint
from bagley.commands.machines import running_server


class Progress:
    """Prints bench events as they arrive, as ctOS terminal lines."""

    def __init__(self, paint: Paint, *, quiet: bool = False) -> None:
        self.paint = paint
        self.quiet = quiet

    def __call__(self, event: dict[str, Any]) -> None:
        if self.quiet:
            return
        p, kind = self.paint, event["type"]
        if kind == "bench.start":
            print(
                p.gray(f"BENCH {event['run_id'].upper()} // ")
                + p.white(f"{len(event['tasks'])} TASKS // {event['total']} RUNS")
            )
            for machine in event["machines"]:
                if machine["note"]:
                    print(p.white(machine["name"]) + p.gray(f" // SKIP  {machine['note']}"))
        elif kind == "bench.task":
            print(self.task_line(event))
        elif kind == "bench.model" and event["result"]["error"]:
            print(
                p.white(f"{event['machine']} {event['model']} ")
                + p.bad("LOAD FAIL  ")
                + p.gray(event["result"]["error"])
            )
        elif kind == "error":
            print(p.bad("[CRIT] ") + event["message"])
        sys.stdout.flush()

    def task_line(self, event: dict[str, Any]) -> str:
        """``[03/10] H4CH1 qwen3:14b weather_lisbon .... OK 1.8s``"""
        p = self.paint
        digits = max(2, len(str(event["total"])))
        label = f"{event['machine']} {event['model']} {event['task']}"
        dots = "." * max(4, 52 - len(label))
        line = (
            p.gray(f"[{event['index']:0{digits}d}/{event['total']:0{digits}d}] ")
            + p.white(event["machine"])
            + p.body(f" {event['model']} {event['task']} ")
            + p.gray(dots + " ")
            + (p.ok("OK") if event["ok"] else p.bad("FAIL"))
            + p.body(f" {event['seconds']:.1f}s")
        )
        return line if event["ok"] else line + p.gray(f"  {event['reason']}")


def table(result: dict[str, Any], paint: Paint) -> list[str]:
    """One block per machine: MODEL, PASS, AVG, TOK/S, SCORE, the winner marked ``>``."""
    lines: list[str] = []
    for machine in result["machines"]:
        header = f"{machine['name']} // {machine['role'].upper()}"
        if not machine["models"]:
            lines.append(paint.white(header) + paint.gray(f" // SKIPPED  {machine['note']}"))
            continue
        lines.append(paint.white(header))
        width = max([5, *(len(m["model"]) for m in machine["models"])]) + 2
        lines.append(
            paint.gray(f"   {'MODEL':<{width}}{'PASS':>6}{'AVG':>8}{'TOK/S':>8}{'SCORE':>8}")
        )
        for m in sorted(machine["models"], key=lambda m: (-m["score"], m["avg_seconds"] or 0)):
            mark = "> " if m["winner"] else "  "
            name = f" {m['model']:<{width}}"
            if m["error"]:
                lines.append(
                    paint.gray(mark)
                    + paint.body(name)
                    + paint.bad("LOAD FAIL  ")
                    + paint.gray(m["error"])
                )
                continue
            avg = f"{m['avg_seconds']:.1f}s" if m["avg_seconds"] is not None else "--"
            tps = f"{m['tokens_per_second']:.1f}" if m["tokens_per_second"] else "--"
            figures = f"{m['passed']:02d}/{m['total']:02d}".rjust(6) + f"{avg:>8}{tps:>8}"
            score = f"{m['score']:>8.1f}"
            color = paint.white if m["winner"] else paint.body
            lines.append(
                paint.white(mark)
                + color(name)
                + (paint.ok if m["passed"] == m["total"] else paint.body)(figures)
                + color(score)
            )
    return lines


def _run_local(body: dict[str, Any], progress: Progress) -> dict[str, Any]:
    from bagley.bench import apply_winners, run_bench
    from bagley.config import ServerConfig
    from bagley.runtime import Runtime

    async def main() -> dict[str, Any]:
        rt = Runtime(ServerConfig.from_env())
        try:
            result = await run_bench(
                rt,
                machines=body["machines"] or None,
                models=body["models"] or None,
                tasks=body["tasks"] or None,
                progress=progress,
            )
            applied = apply_winners(rt, result) if body["apply"] else None
            return {"type": "bench.done", "result": result.to_dict(), "applied": applied}
        finally:
            await rt.aclose()

    return asyncio.run(main())


def cmd_bench(args: argparse.Namespace) -> int:
    from bagley.bench import TASKS, select_tasks

    paint = Paint()
    if args.list_tasks:
        for task in TASKS:
            print(paint.white(f"{task.id:<18}") + paint.gray(f"{task.title:<30}") + task.prompt)
        return 0
    body = {
        "machines": args.machine or [],
        "models": args.models or [],
        "tasks": args.tasks or [],
        "apply": args.apply,
    }
    progress = Progress(paint, quiet=args.json)
    done: dict[str, Any] | None = None
    try:
        select_tasks(body["tasks"])
        if client := running_server():
            with client:
                for event in client.stream("/api/bench/run", body):
                    progress(event)
                    if event["type"] == "bench.done":
                        done = event
                    elif event["type"] == "error" and args.json:
                        print(json.dumps(event), file=sys.stderr)
        else:
            done = _run_local(body, progress)
    except (ValueError, ServerUnavailable) as exc:
        print(f"[CRIT] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    if done is None:
        return 1
    result, applied = done["result"], done.get("applied")
    if args.json:
        print(json.dumps({"result": result, "applied": applied}, ensure_ascii=False))
    else:
        print()
        for line in table(result, paint):
            print(line)
        for machine_id, model in (applied or {}).get("applied", {}).items():
            print(paint.ok("[OK] ") + f"APPLIED  {machine_id.upper()} -> {model}")
        for machine_id, reason in (applied or {}).get("skipped", {}).items():
            print(paint.bad("[WARN] ") + f"NOT APPLIED  {machine_id.upper()}  {reason}")
        if applied is None and any(m["winner"] for m in result["machines"]):
            print(paint.gray("Run with --apply to make the winners the default models."))
    tested = any(m["models"] for m in result["machines"])
    return 0 if tested else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "bench", help="Test the installed models on tool-use tasks and pick the best per machine"
    )
    p.add_argument("--machine", action="append", metavar="ID", help="Only this machine (repeat)")
    p.add_argument(
        "--models",
        nargs="+",
        metavar="GLOB",
        help="Only models matching, e.g. 'qwen3*' gpt-oss:20b",
    )
    p.add_argument("--tasks", nargs="+", metavar="ID", help="Only these tasks (see --list-tasks)")
    p.add_argument("--apply", action="store_true", help="Make each machine's winner its default")
    p.add_argument("--json", action="store_true", help="Print the result as JSON")
    p.add_argument("--list-tasks", action="store_true", help="List the tasks and exit")
    p.set_defaults(func=cmd_bench)
