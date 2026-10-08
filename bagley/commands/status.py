"""``bagley status`` and ``bagley approve``: what Bagley is doing, and answering approvals.

``bagley status --follow --json`` prints one JSON line per change; the Quickshell bar segment
reads it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from bagley.client import Client, ServerUnavailable


def line(snap: dict) -> str:
    machine = f" {snap['machine']}" if snap.get("machine") else ""
    tool = f" {snap['tool']}" if snap.get("tool") else ""
    waiting = f" AWAIT×{snap['approvals']}" if snap.get("approvals") else ""
    return f"{snap.get('code', '?')}{tool}{machine}{waiting}"


def cmd_status(args: argparse.Namespace) -> int:
    client = Client()
    retry = 2.0
    while True:
        try:
            if not args.follow:
                snap = client.get("/api/activity")
                print(json.dumps(snap) if args.json else line(snap))
                return 0
            for snap in client.follow_activity():
                if snap.get("type") == "ping":
                    continue
                print(json.dumps(snap) if args.json else line(snap), flush=True)
                retry = 2.0
        except ServerUnavailable as exc:
            offline = {"state": "offline", "code": "NO SIGNAL", "machine": "", "error": str(exc)}
            if not args.follow:
                print(json.dumps(offline) if args.json else "NO SIGNAL", file=sys.stdout)
                return 1
            print(json.dumps(offline) if args.json else "NO SIGNAL", flush=True)
            time.sleep(retry)  # The server may be restarting; keep following.
            retry = min(retry * 1.5, 15.0)
        except KeyboardInterrupt:
            return 0


def cmd_approve(args: argparse.Namespace) -> int:
    client = Client()
    try:
        if not args.id:
            pending = client.get("/api/approvals")
            if args.json:
                print(json.dumps(pending))
            for item in pending if not args.json else []:
                print(f"{item['id']}  {item['tool']}  {item['summary']}  [{item['source']}]")
            return 0
        client.decide(args.id, "deny" if args.deny else "always" if args.always else "allow")
    except ServerUnavailable as exc:
        print(f"Bagley is not reachable: {exc}", file=sys.stderr)
        return 1
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    status = sub.add_parser("status", help="What Bagley is doing (for status bars)")
    status.add_argument("-f", "--follow", action="store_true", help="Print every change")
    status.add_argument("--json", action="store_true", help="One JSON object per line")
    status.set_defaults(func=cmd_status)

    approve = sub.add_parser("approve", help="List or answer pending approvals")
    approve.add_argument("id", nargs="?", help="Approval id (omit to list)")
    group = approve.add_mutually_exclusive_group()
    group.add_argument("--deny", action="store_true", help="Deny instead of allowing")
    group.add_argument("--always", action="store_true", help="Allow this tool for the session")
    approve.add_argument("--json", action="store_true", help="List as JSON")
    approve.set_defaults(func=cmd_approve)
