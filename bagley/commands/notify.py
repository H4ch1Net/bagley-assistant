"""``bagley notify test``: send a test notification to the desktop and the phone and say which
of them took it.

    bagley notify test [--phone] [--desktop]
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from bagley.commands.runner import run, via
from bagley.readout import Readout

if TYPE_CHECKING:
    from bagley.runtime import Runtime


def cmd_test(args: argparse.Namespace) -> int:
    chosen = [t for t, on in (("desktop", args.desktop), ("phone", args.phone)) if on]
    targets = chosen or ["desktop", "phone"]

    async def local(rt: Runtime) -> Any:
        from bagley.api.notifications import send_test

        return await send_test(rt, targets)

    def command(out: Readout) -> int:
        result = via("POST", "/api/notify/test", {"targets": targets}, local=local)
        out.row("LEVEL", result["level"].upper())
        out.row("WINDOWS", f"{result['windows']:03d}")
        for target, sent in result["results"].items():
            out.row(target, sent["detail"], "OK" if sent["ok"] else "CRIT")
        return 0 if all(r["ok"] for r in result["results"].values()) else 1

    return run(command)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("notify", help="Notifications to the desktop and the phone")
    actions = p.add_subparsers(dest="notify_command", required=True)
    test = actions.add_parser("test", help="Send a test notification")
    test.add_argument("--phone", action="store_true", help="Only the phone (ntfy)")
    test.add_argument("--desktop", action="store_true", help="Only the desktop (notify-send)")
    test.set_defaults(func=cmd_test)
