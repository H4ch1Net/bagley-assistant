"""``bagley machines``: the machines Bagley routes to, their health and the routing order.

    bagley machines [--json] [--fresh]
    bagley machines add NAME [URL] [--preset claude|openai|openrouter|...] [--role ...]
                        [--provider ...] [--model M] [--key-env VAR]
    bagley machines remove ID
    bagley machines route [--purpose chat|light|vision]

Uses the running server when there is one, otherwise works on the settings in this process.
An API key is only ever read from an environment variable (``--key-env``), never from the
command line, where it would end up in the shell history and the process list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from pydantic import ValidationError

from bagley.client import Client, ServerUnavailable
from bagley.commands.audit import Paint
from bagley.config import Machine, ServerConfig, normalize_base_url
from bagley.llm.presets import PRESETS

T = TypeVar("T")


def _local(work: Callable[[Any], Awaitable[T]]) -> T:
    """Run ``work(runtime)`` in this process, for when no server is running."""
    from bagley.runtime import Runtime

    async def main() -> T:
        rt = Runtime(ServerConfig.from_env())
        try:
            return await work(rt)
        finally:
            await rt.aclose()

    return asyncio.run(main())


def running_server() -> Client | None:
    client = Client()
    if client.available():
        return client
    client.close()
    return None


def machine_id(name: str) -> str:
    """A machine id from its name: "H4CH1 Desktop" -> "h4ch1-desktop"."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    return slug[:32].rstrip("-_")


# Listing ----------------------------------------------------------------------------------------


def status(fresh: bool = False) -> dict[str, Any]:
    """Machines with their health and URL, plus the routing settings. In this process every
    machine is probed; the server answers from its cache unless ``fresh``."""
    if client := running_server():
        with client:
            data = client.get("/api/machines", fresh=str(fresh).lower())
            values = client.get("/api/preferences")["values"]
        urls = {"local": values["base_url"], **{m["id"]: m["base_url"] for m in values["machines"]}}
        for info in data["machines"]:
            info["base_url"] = urls.get(info["id"], "")
        return data

    async def work(rt: Any) -> dict[str, Any]:
        prefs, _ = rt.preferences()
        urls = {m.id: m.base_url for m in rt.router.machines(prefs, enabled_only=False)}
        machines = await rt.router.status(prefs, fresh=True)
        for info in machines:
            info["base_url"] = urls.get(info["id"], "")
        return {"routing": prefs.routing, "light_local": prefs.light_local, "machines": machines}

    return _local(work)


def health(info: dict[str, Any], paint: Paint) -> str:
    """``ONLINE 12ms`` or ``OFFLINE <reason>``."""
    if info.get("ok"):
        return paint.ok(f"ONLINE {info.get('latency_ms', 0)}ms")
    reason = str(info.get("error") or "offline")
    reason = reason if len(reason) <= 60 else reason[:59] + "…"
    return paint.bad("OFFLINE ") + paint.gray(reason)


def render(data: dict[str, Any], paint: Paint) -> list[str]:
    """The machines in routing order (disabled ones last) as a ctOS table."""
    machines = sorted(
        data["machines"], key=lambda m: (m.get("priority") is None, m.get("priority") or 0)
    )
    light = "LIGHT LOCAL" if data.get("light_local") else "LIGHT ROUTED"
    lines = [paint.gray(f"ROUTING {str(data.get('routing', 'auto')).upper()} // {light}")]

    def width(key: str) -> int:
        return max([len(key), *(len(str(m.get(key) or "auto")) for m in machines)]) + 2

    id_w, name_w, url_w, model_w = width("id"), width("name"), width("base_url"), width("model")
    lines.append(
        paint.gray(
            f"{'#':<4}{'ID':<{id_w}}{'NAME':<{name_w}}{'ROLE':<7}{'URL':<{url_w}}"
            f"{'MODELS':>6}  {'MODEL':<{model_w}}STATUS"
        )
    )
    for m in machines:
        rank = "--" if m.get("priority") is None else f"{m['priority'] + 1:02d}"
        lines.append(
            paint.body(f"{rank:<4}")
            + paint.white(f"{m['id']:<{id_w}}")
            + paint.white(f"{m['name']:<{name_w}}")
            + paint.body(f"{m['role'].upper():<7}")
            + paint.gray(f"{m.get('base_url', ''):<{url_w}}")
            + paint.body(f"{len(m.get('models') or []):>6}  ")
            + paint.white(f"{m.get('model') or 'auto':<{model_w}}")
            + health(m, paint)
        )
    return lines


def cmd_list(args: argparse.Namespace) -> int:
    try:
        data = status(fresh=args.fresh)
    except ServerUnavailable as exc:
        print(f"[CRIT] Bagley is not reachable: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    for line in render(data, Paint()):
        print(line)
    return 0


# Changing ---------------------------------------------------------------------------------------


def _save(change: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]) -> None:
    """Apply ``change(machines, values)`` to the saved machines; it returns the preference
    changes to store. Goes through the server when it runs, so open windows see it."""
    if client := running_server():
        with client:
            values = client.get("/api/preferences")["values"]
            body = change([dict(m) for m in values["machines"]], values)
            try:
                resp = client.http.put("/api/preferences", json=body)
            except httpx.HTTPError as exc:
                raise ServerUnavailable(str(exc)) from exc
            if resp.status_code >= 400:
                raise ValueError(resp.json().get("detail", resp.text))
        return

    async def work(rt: Any) -> None:
        prefs, _ = rt.preferences()
        values = prefs.model_dump()
        rt.update_preferences(change([dict(m) for m in values["machines"]], values))

    _local(work)


def cmd_add(args: argparse.Namespace) -> int:
    preset = PRESETS.get(args.preset or "", {})
    url = args.url or preset.get("base_url", "")
    if not url:
        print("[CRIT] Give the server URL, or a hosted API with --preset.", file=sys.stderr)
        return 2
    provider = args.provider or preset.get("provider", "auto")
    key = ""
    if args.key_env:
        key = os.environ.get(args.key_env, "").strip()
        if not key:
            print(f"[CRIT] {args.key_env} is not set or empty.", file=sys.stderr)
            return 2
    entry = {
        "id": machine_id(args.id or args.name),
        "name": args.name.strip()[:32],
        "role": args.role or ("cloud" if preset else "gpu"),
        "provider": provider,
        "base_url": url if provider in ("openai", "anthropic") else normalize_base_url(url),
        "api_key": key,
        "model": args.model or preset.get("model", ""),
    }
    try:
        Machine.model_validate(entry)
    except ValidationError as exc:
        error = exc.errors()[0]
        print(f"[CRIT] {error['loc'][0]}: {error['msg']}", file=sys.stderr)
        return 2
    if entry["id"] == "local":
        print("[CRIT] 'local' is this machine; pick another name.", file=sys.stderr)
        return 2

    def change(machines: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
        if any(m["id"] == entry["id"] for m in machines):
            raise ValueError(f"A machine with id '{entry['id']}' already exists.")
        return {"machines": [*machines, entry]}

    try:
        _save(change)
    except (ValueError, ServerUnavailable) as exc:
        print(f"[CRIT] {exc}", file=sys.stderr)
        return 1
    paint = Paint()
    env_key = preset.get("key_env", "")
    print(
        paint.ok("[OK] ")
        + f"MACHINE {entry['id']} ADDED  {entry['role'].upper()}  {entry['base_url']}"
        + ("  KEY " + args.key_env if key else "")
    )
    if preset and not key and not os.environ.get(env_key):
        print(f"No key saved. Set {env_key} where Bagley runs, or add it in Settings.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    if args.id == "local":
        print("[CRIT] This machine can't be removed.", file=sys.stderr)
        return 2

    def change(machines: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
        kept = [m for m in machines if m["id"] != args.id]
        if len(kept) == len(machines):
            raise ValueError(f"No machine with id '{args.id}'.")
        changes: dict[str, Any] = {"machines": kept}
        if values.get("routing") == args.id:  # Pinned to it: go back to automatic routing.
            changes["routing"] = "auto"
        return changes

    try:
        _save(change)
    except (ValueError, ServerUnavailable) as exc:
        print(f"[CRIT] {exc}", file=sys.stderr)
        return 1
    print(Paint().ok("[OK] ") + f"MACHINE {args.id} REMOVED")
    return 0


def route(purpose: str) -> dict[str, Any]:
    """Which machine and model would answer now."""
    if client := running_server():
        with client:
            return client.get("/api/machines/route", purpose=purpose)

    async def work(rt: Any) -> dict[str, Any]:
        from bagley.llm import LLMError

        prefs, _ = rt.preferences()
        try:
            chosen = await rt.router.choose(prefs, purpose)
        except LLMError as exc:
            return {"ok": False, "error": exc.message, "hint": exc.hint}
        return {"ok": True, **chosen.describe()}

    return _local(work)


def cmd_route(args: argparse.Namespace) -> int:
    try:
        data = route(args.purpose)
    except ServerUnavailable as exc:
        print(f"[CRIT] Bagley is not reachable: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return 0 if data.get("ok") else 1
    paint = Paint()
    if not data.get("ok"):
        print(paint.bad("[CRIT] NO ROUTE  ") + paint.body(data.get("error", "")))
        if data.get("hint"):
            print(paint.gray(data["hint"]))
        return 1
    print(
        paint.gray(f"{args.purpose.upper()} -> ")
        + paint.white(f"{data['machine']} ")
        + paint.gray(f"// {data['role'].upper()} // ")
        + paint.white(data["model"])
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("machines", help="Machines Bagley routes to, their health and routing")
    p.add_argument("--json", action="store_true", help="Print JSON")
    p.add_argument("--fresh", action="store_true", help="Probe every machine now")
    p.set_defaults(func=cmd_list)
    actions = p.add_subparsers(dest="action")

    add = actions.add_parser("add", help="Add a model server, e.g. a desktop GPU over Tailscale")
    add.add_argument("name", help="Display name, e.g. H4CH1")
    add.add_argument(
        "url", nargs="?", help="Server URL, e.g. http://h4ch1:11434 (not needed with --preset)"
    )
    add.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="A hosted API: sets the URL, server type and role cloud (claude: Claude Opus 5.5)",
    )
    add.add_argument("--id", help="Machine id (default: from the name)")
    add.add_argument("--role", choices=["gpu", "local", "cloud"], help="Default: gpu, or cloud")
    add.add_argument("--provider", choices=["auto", "ollama", "openai", "anthropic"])
    add.add_argument("--model", help="Model to use there (default: picked automatically)")
    add.add_argument("--key-env", metavar="VAR", help="Read the API key from this variable")
    add.set_defaults(func=cmd_add)

    remove = actions.add_parser("remove", help="Remove a machine")
    remove.add_argument("id", help="Machine id")
    remove.set_defaults(func=cmd_remove)

    which = actions.add_parser("route", help="Which machine and model would answer now")
    which.add_argument("--purpose", choices=["chat", "light", "vision"], default="chat")
    which.add_argument("--json", action="store_true", help="Print JSON")
    which.set_defaults(func=cmd_route)
