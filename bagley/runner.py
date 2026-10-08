"""Sync with an always-on runner: another Bagley (a Surface on the tailnet) that keeps running
automations while this machine sleeps.

* ``export_data`` / ``import_data`` move automations, memories and routines between machines as
  JSON. Exports leave out ids, chats, state and run history; imports merge and skip duplicates.
* ``probe`` checks the runner at ``prefs.runner_url``; ``move`` re-creates an automation there and
  only then disables it here. ``pull`` imports the runner's automations here, disabled, for
  reference.

The runner's token (``prefs.runner_token``) is sent to the runner URL and nowhere else.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from bagley import __version__, automations
from bagley.automations import ScheduleError, parse_schedule

if TYPE_CHECKING:
    from bagley.config import Preferences
    from bagley.runtime import Runtime

EXPORT_VERSION = 1
PARTS = ("automations", "memories", "routines")
MAX_ITEMS = {"automations": 1000, "memories": 5000, "routines": 1000}
MAX_RESPONSE_BYTES = 20_000_000
TIMEOUT = httpx.Timeout(10.0, connect=4.0)
# Routine columns that describe one machine's runs rather than the routine itself.
ROUTINE_LOCAL = {"id", "conversation_id", "state", "next_run", "last_run", "last_status",
                 "last_result", "created_at", "updated_at"}  # fmt: skip
SCALAR = (str, int, float, bool, type(None))


class DataError(ValueError):
    """An export that can't be imported."""


class RunnerError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# Export and import ------------------------------------------------------------------------------


class AutomationIn(BaseModel):
    kind: str = Field(pattern=r"^[a-z_]{1,24}$")
    name: str = Field(default="", max_length=120)
    prompt: str = Field(default="", max_length=4000)
    schedule: str = Field(min_length=1, max_length=120)
    target: str | None = Field(default=None, max_length=2000)
    enabled: bool = True


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=500)


def _plain(value: Any) -> bool:
    return isinstance(value, SCALAR) and not (isinstance(value, str) and len(value) > 20_000)


def _has_table(rt: Runtime, name: str) -> bool:
    row = rt.store.query_one(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return row is not None


def _finished(item: dict[str, Any]) -> bool:
    """A one-time automation whose time has passed: nothing left to run anywhere."""
    try:
        sched = parse_schedule(item["schedule"])
    except ScheduleError:
        return False
    return sched.kind == "once" and sched.at is not None and sched.at.timestamp() <= time.time()


def _name(kind: str, name: str, prompt: str) -> str:
    """The name ``Scheduler.create`` stores for what the API is given."""
    return " ".join((name or prompt[:60]).split())[:80] or kind.title()


def _automation_key(kind: str, name: str, schedule: str, target: str | None) -> tuple[str, ...]:
    return (kind, name.casefold(), schedule, target or "")


def _memory_key(text: str) -> str:
    return " ".join(text.split())[:500].casefold()


def export_data(rt: Runtime, parts: list[str] | tuple[str, ...] = PARTS) -> dict[str, Any]:
    unknown = [p for p in parts if p not in PARTS]
    if unknown:
        raise DataError(f"Unknown part {unknown[0]!r}; use {', '.join(PARTS)}.")
    data: dict[str, Any] = {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bagley": __version__,
    }
    if "automations" in parts:
        data["automations"] = [
            {k: item[k] for k in ("kind", "name", "prompt", "schedule", "target", "enabled")}
            for item in rt.store.list_automations()
            if not _finished(item)
        ]
    if "memories" in parts:
        data["memories"] = [{"content": m["content"]} for m in rt.store.list_memories()]
    if "routines" in parts and _has_table(rt, "routines"):
        rows = rt.store.query("SELECT * FROM routines ORDER BY rowid")
        data["routines"] = [
            {k: v for k, v in row.items() if k not in ROUTINE_LOCAL and isinstance(v, SCALAR)}
            for row in rows
        ]
    return data


def _list(data: dict[str, Any], part: str) -> list[Any]:
    items = data.get(part) or []
    if not isinstance(items, list):
        raise DataError(f"'{part}' must be a list.")
    if len(items) > MAX_ITEMS[part]:
        raise DataError(f"Too many {part} (at most {MAX_ITEMS[part]}).")
    return items


def _validated(model: type[BaseModel], part: str, items: list[Any]) -> list[Any]:
    out = []
    for n, raw in enumerate(items):
        if part == "memories" and isinstance(raw, str):
            raw = {"content": raw}
        try:
            out.append(model.model_validate(raw))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise DataError(f"{part}[{n}].{where}: {first['msg']}") from exc
    return out


def import_data(
    rt: Runtime,
    data: Any,
    *,
    enabled: bool | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge an export into this Bagley and count what was added and skipped. The whole shape
    is checked before anything is written; items this machine can't take (an unknown kind, a
    time that has passed) are skipped and listed in ``errors``.

    ``enabled`` overrides each automation's own flag; ``state`` is stored on new automations."""
    if not isinstance(data, dict):
        raise DataError("Expected a JSON object.")
    version = data.get("version", EXPORT_VERSION)
    if not isinstance(version, int) or not 1 <= version <= EXPORT_VERSION:
        raise DataError(f"Unsupported export version {version!r}.")
    if data.get("mode", "merge") != "merge":
        raise DataError("Only mode 'merge' is supported.")
    incoming = _validated(AutomationIn, "automations", _list(data, "automations"))
    memories = _validated(MemoryIn, "memories", _list(data, "memories"))
    routines = _list(data, "routines")
    for n, row in enumerate(routines):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise DataError(f"routines[{n}] needs a name.")
        if not all(_plain(v) for v in row.values()):
            raise DataError(f"routines[{n}] has a value that isn't short text or a number.")

    added = dict.fromkeys(PARTS, 0)
    skipped = dict.fromkeys(PARTS, 0)
    errors: list[dict[str, str]] = []

    existing = {
        _automation_key(a["kind"], a["name"], a["schedule"], a["target"])
        for a in rt.store.list_automations()
    }
    for item in incoming:
        name = _name(item.kind, item.name, item.prompt)
        try:
            schedule = automations.preview(item.schedule, item.kind)["schedule"]
            key = _automation_key(item.kind, name, schedule, item.target)
            if key in existing:
                skipped["automations"] += 1
                continue
            created = rt.scheduler.create(item.kind, name, schedule, item.prompt, item.target)
        except ScheduleError as exc:
            errors.append({"part": "automations", "name": name, "error": str(exc)})
            continue
        existing.add(key)
        added["automations"] += 1
        on = item.enabled if enabled is None else enabled
        if not on or state:
            rt.store.update_automation(created["id"], enabled=on, state=state or {})

    known = {_memory_key(m["content"]) for m in rt.store.list_memories()}
    for memory in memories:
        key = _memory_key(memory.content)
        if not key or key in known:
            skipped["memories"] += 1
            continue
        rt.store.add_memory(memory.content)
        known.add(key)
        added["memories"] += 1

    if routines:
        if _has_table(rt, "routines"):
            _import_routines(rt, routines, enabled, added, skipped, errors)
        else:
            skipped["routines"] = len(routines)
            errors.append({"part": "routines", "name": "", "error": "Routines aren't set up here."})
    return {"ok": True, "added": added, "skipped": skipped, "errors": errors}


def _import_routines(
    rt: Runtime,
    rows: list[dict[str, Any]],
    enabled: bool | None,
    added: dict[str, int],
    skipped: dict[str, int],
    errors: list[dict[str, str]],
) -> None:
    """Insert routines by name, into whichever columns this machine's routines table has."""
    columns = {c["name"]: c for c in rt.store.query("PRAGMA table_info(routines)")}
    if "name" not in columns:
        skipped["routines"] += len(rows)
        errors.append({"part": "routines", "name": "", "error": "Routines here have no names."})
        return
    names = {str(r["name"]).casefold() for r in rt.store.query("SELECT name FROM routines")}
    for row in rows:
        if row["name"].casefold() in names:
            skipped["routines"] += 1
            continue
        values = {k: v for k, v in row.items() if k in columns and k not in ROUTINE_LOCAL}
        if enabled is not None and "enabled" in columns:
            values["enabled"] = int(enabled)
        for col, info in columns.items():
            if col not in values and col.endswith("_at") and info["notnull"] and not info["pk"]:
                values[col] = time.time()
        missing = [
            c
            for c, info in columns.items()
            if info["notnull"] and info["dflt_value"] is None and not info["pk"] and c not in values
        ]
        if missing:
            errors.append({"part": "routines", "name": row["name"], "error": f"No {missing[0]}."})
            continue
        cols = list(values)
        rt.store.execute(
            f"INSERT INTO routines ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(values[c] for c in cols),
        )
        names.add(row["name"].casefold())
        added["routines"] += 1


# The runner -------------------------------------------------------------------------------------


@dataclass
class Target:
    url: str
    token: str


def target(prefs: Preferences) -> Target:
    url = prefs.runner_url.strip().rstrip("/")
    if not url:
        raise RunnerError("No runner configured (Settings, or `bagley runner set URL`).", 409)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query:
        raise RunnerError("The runner URL must look like https://surface.tailnet.ts.net.", 422)
    return Target(url=url, token=prefs.runner_token)


async def request(rt: Runtime, to: Target, method: str, path: str, body: Any = None) -> Any:
    """Call the runner's API. Only this function sends the runner token, and only to its URL;
    redirects are not followed, so the token can't be bounced elsewhere."""
    headers = {"Accept": "application/json"}
    if to.token:
        headers["Authorization"] = f"Bearer {to.token}"
    try:
        async with rt.http.stream(
            method,
            to.url + path,
            json=body,
            headers=headers,
            timeout=TIMEOUT,
            follow_redirects=False,
        ) as resp:
            raw = bytearray()
            async for chunk in resp.aiter_bytes():
                raw += chunk
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RunnerError("The runner's answer is too large.")
    except httpx.TimeoutException as exc:
        raise RunnerError("The runner did not answer in time.") from exc
    except httpx.HTTPError as exc:
        raise RunnerError(
            f"Can't reach the runner: {exc.__class__.__name__} {exc}".strip()
        ) from exc
    if resp.status_code == 401:
        raise RunnerError("The runner refused the token. Set runner_token to its BAGLEY_TOKEN.")
    if resp.status_code == 403:
        raise RunnerError(
            f"The runner refused this machine: {bytes(raw[:200]).decode(errors='replace')}"
        )
    if 300 <= resp.status_code < 400:
        raise RunnerError("The runner redirected the request. Check the runner URL.")
    try:
        payload = json.loads(bytes(raw)) if raw else None
    except ValueError as exc:
        raise RunnerError(f"The runner answered HTTP {resp.status_code}, not Bagley JSON.") from exc
    if resp.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise RunnerError(f"The runner answered HTTP {resp.status_code}: {detail or 'error'}")
    return payload


async def probe(rt: Runtime) -> dict[str, Any]:
    """Is the runner there, which version, how many automations, how fast."""
    prefs, _ = rt.preferences()
    out: dict[str, Any] = {
        "configured": bool(prefs.runner_url.strip()),
        "url": prefs.runner_url.strip().rstrip("/"),
        "reachable": False,
        "version": None,
        "automations": None,
        "latency_ms": None,
        "error": None,
    }
    try:
        to = target(prefs)
        start = time.perf_counter()
        info = await request(rt, to, "GET", "/api/info")
        out["latency_ms"] = round((time.perf_counter() - start) * 1000)
        out["reachable"] = True
        out["version"] = info.get("version") if isinstance(info, dict) else None
        items = await request(rt, to, "GET", "/api/automations")
        out["automations"] = len(items) if isinstance(items, list) else None
    except RunnerError as exc:
        out["error"] = exc.message
    return out


async def move(rt: Runtime, aid: int, *, to: Target | None = None) -> dict[str, Any]:
    """Create automation ``aid`` on the runner, then disable it here and note where it went.
    If the runner doesn't take it, nothing changes here."""
    item = rt.store.get_automation(aid)
    if not item:
        raise RunnerError("Automation not found.", 404)
    to = to or target(rt.preferences()[0])
    moved_to = (item.get("state") or {}).get("moved_to")
    if moved_to and not item["enabled"]:
        raise RunnerError(f"Already moved to {moved_to}.", 409)
    if aid in rt.scheduler.running:
        raise RunnerError("It is running right now. Try again when it's done.", 409)
    body = {k: item[k] for k in ("kind", "name", "prompt", "schedule", "target")}
    created = await request(rt, to, "POST", "/api/automations", body)
    runner_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(runner_id, int):
        raise RunnerError("The runner did not confirm the automation.")
    state = {**(item.get("state") or {}), "moved_to": to.url, "runner_id": runner_id}
    rt.store.update_automation(aid, enabled=False, state={**state, "moved_at": time.time()})
    await rt.broadcast({"type": "automations.changed"})
    return {"ok": True, "id": aid, "name": item["name"], "runner_id": runner_id, "runner": to.url}


async def push(rt: Runtime, ids: list[int] | None = None) -> dict[str, Any]:
    """Move several automations (by default every enabled one) and report each."""
    to = target(rt.preferences()[0])
    await request(rt, to, "GET", "/api/info")  # Fail once, not once per automation.
    if ids is None:
        ids = [a["id"] for a in rt.store.list_automations() if a["enabled"]]
    moved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for aid in ids:
        try:
            moved.append(await move(rt, aid, to=to))
        except RunnerError as exc:
            item = rt.store.get_automation(aid) or {}
            failed.append({"id": aid, "name": item.get("name", ""), "error": exc.message})
    return {"ok": not failed, "moved": moved, "failed": failed}


async def pull(rt: Runtime) -> dict[str, Any]:
    """Import the runner's automations, memories and routines here. Automations arrive
    disabled, as a reference of what runs there."""
    to = target(rt.preferences()[0])
    data = await request(rt, to, "GET", "/api/export?parts=" + ",".join(PARTS))
    try:
        result = import_data(rt, data, enabled=False, state={"runner_copy": to.url})
    except DataError as exc:
        raise RunnerError(f"The runner's export can't be imported: {exc}") from exc
    await rt.broadcast({"type": "automations.changed"})
    return {**result, "runner": to.url}
