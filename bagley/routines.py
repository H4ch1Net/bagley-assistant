"""Routines: named, ordered lists of tool calls with fixed arguments, saved once and run again
with one approval.

Approval works per routine, not per step:

* Saving a routine approves its exact steps: in the UI, through the API, or with the
  ``save_routine`` tool, which asks first and shows the steps.
* Running it from the UI or ``bagley routine run`` asks once for the whole routine (the steps
  are shown), then runs them without asking again. The ``run_routine`` tool asks once too.
* A scheduled routine (the ``routine`` automation kind) runs the same saved steps with nobody
  watching. Nothing in between can change them: there is no model in the loop.

Permission tiers still apply at run time: a step whose tool is set to "deny", or is no longer
installed, fails with the reason. A run stops at the first failed step unless that step says
``continue_on_error``, writes every step to the audit log, reports progress through ``emit``
and posts a summary into the "Routines" chat.

A routine can also be recorded: start recording in a chat, ask Bagley to do things, stop, and
every tool call that succeeded in between becomes a step. ``candidates`` lists a chat's
successful tool calls so the UI can let the user tick the ones to keep.

Scenes ("set up coding mode") are routines too; the built-in "coding" scene is seeded the first
time routines are used on a computer with the desktop tools.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bagley.approvals import summarize
from bagley.permissions import permission_for
from bagley.tools import ToolError

if TYPE_CHECKING:
    from bagley.config import Preferences
    from bagley.runtime import Runtime
    from bagley.store import Store
    from bagley.tools import Registry

log = logging.getLogger("bagley.routines")

SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    steps       TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    last_run    REAL,
    last_status TEXT
);
CREATE TABLE IF NOT EXISTS routine_recordings (
    conversation_id TEXT PRIMARY KEY,
    after_id        INTEGER NOT NULL,
    started_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routine_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MAX_STEPS = 20
MAX_NAME = 60
MAX_ARGUMENTS = 8000  # Characters of JSON per step.
RESULT_CHARS = 1000
CHAT_TITLE = "Routines"
# Tools a routine can't contain: routines don't run routines.
NOT_STEPS = {"load_tools", "run_routine", "save_routine", "set_up_scene"}

CODING_SCENE: dict[str, Any] = {
    "name": "coding",
    "description": "Coding scene: kitty, Zed and lazygit on workspace 2, then switch to it.",
    "steps": [
        {"tool": "launch_app", "arguments": {"command": "kitty", "workspace": 2}, "note": ""},
        {"tool": "launch_app", "arguments": {"command": "zed", "workspace": 2}, "note": ""},
        {
            "tool": "launch_app",
            "arguments": {"command": "lazygit", "workspace": 2},
            "note": "In kitty",
        },
        {"tool": "switch_workspace", "arguments": {"workspace": 2}, "note": ""},
    ],
}

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class RoutineError(ValueError):
    """A routine, its steps or a request about it is not valid. The message is for the user."""


@dataclass
class _State:
    running: set[int] = field(default_factory=set)


# Runtimes that have used routines, so the automation kind can check that a routine exists
# (``Kind.validate`` only sees the automation's fields).
_LIVE: weakref.WeakSet[Runtime] = weakref.WeakSet()


def ensure(rt: Runtime) -> _State:
    """Create the tables on first use and seed the built-in scene if the desktop tools exist."""
    state = rt.services.get("routines")
    if state is None:
        rt.store.ensure_schema(SCHEMA)
        state = rt.services["routines"] = _State()
        _LIVE.add(rt)
        _seed(rt)
    return state


def _meta(store: Store, key: str) -> str | None:
    row = store.query_one("SELECT value FROM routine_meta WHERE key = ?", (key,))
    return row["value"] if row else None


def _set_meta(store: Store, key: str, value: str) -> None:
    store.execute(
        "INSERT INTO routine_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _seed(rt: Runtime) -> None:
    """Add the "coding" scene once. Deleting it later keeps it deleted."""
    if not (rt.registry.get("launch_app") and rt.registry.get("switch_workspace")):
        return
    if _meta(rt.store, "seeded.coding"):
        return
    if not rt.store.query_one("SELECT id FROM routines WHERE name = ?", (CODING_SCENE["name"],)):
        _insert(
            rt.store, CODING_SCENE["name"], CODING_SCENE["description"], CODING_SCENE["steps"], True
        )
    _set_meta(rt.store, "seeded.coding", "1")


# Saved routines -----------------------------------------------------------------------------


def _routine(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["steps"] = json.loads(row["steps"] or "[]")
    row["builtin"] = bool(row["builtin"])
    return row


def _insert(
    store: Store, name: str, description: str, steps: list[dict[str, Any]], builtin: bool
) -> int:
    now = time.time()
    try:
        cur = store.execute(
            "INSERT INTO routines (name, description, steps, builtin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, json.dumps(steps), int(builtin), now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise RoutineError(f"A routine called “{name}” already exists.") from exc
    return int(cur.lastrowid or 0)


def clean_name(name: str) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise RoutineError("Give the routine a name.")
    if len(text) > MAX_NAME:
        raise RoutineError(f"Routine names can be up to {MAX_NAME} characters.")
    if text.isdigit():
        raise RoutineError("A routine name needs at least one letter.")
    return text


def validate_steps(registry: Registry, steps: Any) -> list[dict[str, Any]]:
    """Check every step against the tool it names and return the steps as they will run: each
    tool exists, can be part of a routine, and its arguments pass ``Tool.coerce``."""
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except json.JSONDecodeError as exc:
            raise RoutineError("Steps must be a list of {tool, arguments}.") from exc
    if not isinstance(steps, list) or not steps:
        raise RoutineError("A routine needs at least one step.")
    if len(steps) > MAX_STEPS:
        raise RoutineError(f"A routine can have up to {MAX_STEPS} steps.")
    out = []
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise RoutineError(f"Step {number} must be an object with a tool and its arguments.")
        name = str(step.get("tool") or "").strip()
        tool = registry.get(name)
        if tool is None:
            raise RoutineError(f"Step {number}: there is no tool named '{name}'.")
        if name in NOT_STEPS:
            raise RoutineError(f"Step {number}: a routine can't use {name}.")
        args = step.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError as exc:
                raise RoutineError(
                    f"Step {number} ({name}): arguments are not valid JSON."
                ) from exc
        if not isinstance(args, dict):
            raise RoutineError(f"Step {number} ({name}): arguments must be an object.")
        try:
            checked = tool.coerce(args)
        except ToolError as exc:
            raise RoutineError(f"Step {number} ({name}): {exc}") from exc
        if len(json.dumps(checked)) > MAX_ARGUMENTS:
            raise RoutineError(f"Step {number} ({name}): the arguments are too long.")
        item: dict[str, Any] = {
            "tool": name,
            "arguments": checked,
            "note": " ".join(str(step.get("note") or "").split())[:200],
        }
        if step.get("continue_on_error"):
            item["continue_on_error"] = True
        out.append(item)
    return out


def list_routines(rt: Runtime) -> list[dict[str, Any]]:
    ensure(rt)
    rows = rt.store.query("SELECT * FROM routines ORDER BY name COLLATE NOCASE")
    return [_routine(r) for r in rows]


def get_routine(rt: Runtime, key: int | str) -> dict[str, Any] | None:
    """A routine by id, or by name (ignoring case)."""
    ensure(rt)
    text = " ".join(str(key).split())
    row = None
    if text.isdigit():
        row = rt.store.query_one("SELECT * FROM routines WHERE id = ?", (int(text),))
    if row is None and text:
        row = rt.store.query_one("SELECT * FROM routines WHERE name = ?", (text,))
    return _routine(row) if row else None


def find_scene(rt: Runtime, name: str) -> dict[str, Any] | None:
    """The routine for "coding", "coding mode", "the coding scene"..."""
    text = " ".join(name.lower().split())
    short = re.sub(r"^the |\s+(mode|scene|setup|set up|layout)$", "", text).strip()
    for key in (text, short, f"{short} mode", f"{short} scene"):
        if key and (routine := get_routine(rt, key)):
            return routine
    return None


def create_routine(
    rt: Runtime, name: str, description: str = "", steps: Any = (), *, builtin: bool = False
) -> dict[str, Any]:
    ensure(rt)
    rid = _insert(
        rt.store,
        clean_name(name),
        " ".join(description.split())[:500],
        validate_steps(rt.registry, list(steps) if isinstance(steps, tuple) else steps),
        builtin,
    )
    return get_routine(rt, rid) or {}


def update_routine(
    rt: Runtime,
    rid: int,
    *,
    name: str | None = None,
    description: str | None = None,
    steps: Any = None,
) -> dict[str, Any] | None:
    ensure(rt)
    current = rt.store.query_one("SELECT * FROM routines WHERE id = ?", (rid,))
    if current is None:
        return None
    changes: dict[str, Any] = {}
    if name is not None:
        changes["name"] = clean_name(name)
    if description is not None:
        changes["description"] = " ".join(description.split())[:500]
    if steps is not None:
        changes["steps"] = json.dumps(validate_steps(rt.registry, steps))
    changes["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in changes)
    try:
        rt.store.execute(f"UPDATE routines SET {sets} WHERE id = ?", (*changes.values(), rid))
    except sqlite3.IntegrityError as exc:
        raise RoutineError(f"A routine called “{changes.get('name')}” already exists.") from exc
    return get_routine(rt, rid)


def delete_routine(rt: Runtime, rid: int) -> bool:
    """Delete a routine and pause the automations that ran it."""
    ensure(rt)
    if rt.store.execute("DELETE FROM routines WHERE id = ?", (rid,)).rowcount == 0:
        return False
    for item in rt.store.list_automations():
        if item["kind"] == "routine" and str(item.get("target")) == str(rid):
            rt.store.update_automation(
                item["id"],
                enabled=False,
                next_run=None,
                last_status="error",
                last_result="Its routine was deleted.",
            )
    return True


def describe(
    rt: Runtime, routine: dict[str, Any], prefs: Preferences | None = None
) -> dict[str, Any]:
    """A routine for the UI and the CLI: each step with its readable summary, its tool's risk and
    whether it can run now (the tool exists and is not set to "deny")."""
    state = ensure(rt)
    if prefs is None:
        prefs, _ = rt.preferences()
    steps = []
    for step in routine["steps"]:
        tool = rt.registry.get(step["tool"])
        tier = permission_for(tool, prefs) if tool else "deny"
        steps.append(
            {
                **step,
                "summary": summarize(tool.summary if tool else "", step["tool"], step["arguments"]),
                "risk": tool.risk if tool else "safe",
                "available": tier != "deny",
            }
        )
    return {**routine, "steps": steps, "running": routine["id"] in state.running}


def known_routine(rid: int) -> bool | None:
    """Whether routine ``rid`` exists in this process's open runtimes; None when no open
    runtime has used routines yet (the scheduled run checks again)."""
    checked = False
    for rt in list(_LIVE):
        try:
            row = rt.store.query_one("SELECT 1 AS found FROM routines WHERE id = ?", (rid,))
        except sqlite3.Error:
            continue  # That runtime's database is closed.
        checked = True
        if row:
            return True
    return False if checked else None


# The chat runs report to --------------------------------------------------------------------


def conversation(rt: Runtime) -> str:
    """The "Routines" chat, created on first use (and again if the user deletes it)."""
    ensure(rt)
    cid = _meta(rt.store, "conversation")
    if cid and rt.store.get_conversation(cid):
        return cid
    conv = rt.store.create_conversation(CHAT_TITLE)
    _set_meta(rt.store, "conversation", conv["id"])
    return conv["id"]


# Picking steps from a chat ------------------------------------------------------------------


def _arguments(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return {} if raw is None else None


def candidates(
    store: Store, conversation_id: str, *, registry: Registry | None = None, after_id: int = 0
) -> list[dict[str, Any]]:
    """The tool calls in a chat that succeeded, in order, with their message ids, for choosing
    a routine's steps. ``after_id`` keeps only calls made after that message. With a
    ``registry`` each call also says whether its tool still exists, its risk and a summary;
    ``suggested`` marks the calls that changed something (the ones worth repeating)."""
    messages = store.list_messages(conversation_id)
    out: list[dict[str, Any]] = []
    for i, message in enumerate(messages):
        if message["role"] != "assistant" or not message.get("tool_calls"):
            continue
        if message["id"] <= after_id:
            continue
        results: dict[str, dict[str, Any]] = {}
        for reply in messages[i + 1 :]:  # Results directly follow their calls.
            if reply["role"] != "tool":
                break
            results.setdefault(str(reply.get("tool_call_id")), reply)
        for call in message["tool_calls"]:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            result = results.get(str(call.get("id")))
            if not result or not (result.get("meta") or {}).get("ok") or name in NOT_STEPS:
                continue
            args = _arguments(fn.get("arguments"))
            if args is None:
                continue
            item: dict[str, Any] = {
                "message_id": message["id"],
                "result_id": result["id"],
                "call_id": call.get("id"),
                "tool": name,
                "arguments": args,
                "result": (result.get("content") or "")[:300],
                "created_at": message["created_at"],
            }
            if registry is not None:
                tool = registry.get(name)
                item["available"] = tool is not None
                item["risk"] = tool.risk if tool else "safe"
                item["summary"] = summarize(tool.summary if tool else "", name, args)
                item["suggested"] = bool(tool and tool.risk == "confirm")
            out.append(item)
    return out


# Recording ----------------------------------------------------------------------------------


def recording(rt: Runtime, conversation_id: str) -> dict[str, Any]:
    """Whether a chat is recording, and the steps recorded so far."""
    ensure(rt)
    row = rt.store.query_one(
        "SELECT * FROM routine_recordings WHERE conversation_id = ?", (conversation_id,)
    )
    if row is None:
        return {"conversation_id": conversation_id, "recording": False, "steps": []}
    steps = candidates(rt.store, conversation_id, registry=rt.registry, after_id=row["after_id"])
    return {
        "conversation_id": conversation_id,
        "recording": True,
        "after_id": row["after_id"],
        "started_at": row["started_at"],
        "steps": steps,
    }


def start_recording(rt: Runtime, conversation_id: str) -> dict[str, Any]:
    """Record the tool calls this chat makes from now on (restarts a running recording)."""
    ensure(rt)
    if not rt.store.get_conversation(conversation_id):
        raise RoutineError("That chat doesn't exist.")
    last = rt.store.query_one(
        "SELECT COALESCE(MAX(id), 0) AS id FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    )
    rt.store.execute(
        "INSERT INTO routine_recordings (conversation_id, after_id, started_at) VALUES (?, ?, ?) "
        "ON CONFLICT(conversation_id) DO UPDATE SET after_id = excluded.after_id, "
        "started_at = excluded.started_at",
        (conversation_id, int(last["id"]) if last else 0, time.time()),
    )
    return recording(rt, conversation_id)


def cancel_recording(rt: Runtime, conversation_id: str) -> bool:
    ensure(rt)
    cur = rt.store.execute(
        "DELETE FROM routine_recordings WHERE conversation_id = ?", (conversation_id,)
    )
    return cur.rowcount > 0


def stop_recording(
    rt: Runtime, conversation_id: str, name: str, description: str = ""
) -> dict[str, Any]:
    """Save every tool call that succeeded since recording started as a routine. If saving
    fails (the name is taken), the recording keeps going."""
    ensure(rt)
    row = rt.store.query_one(
        "SELECT * FROM routine_recordings WHERE conversation_id = ?", (conversation_id,)
    )
    if row is None:
        raise RoutineError("This chat is not recording.")
    steps = [
        {"tool": c["tool"], "arguments": c["arguments"]}
        for c in candidates(rt.store, conversation_id, after_id=row["after_id"])
    ]
    if not steps:
        raise RoutineError("Nothing to save: no tool call succeeded since recording started.")
    routine = create_routine(rt, name, description, steps)
    cancel_recording(rt, conversation_id)
    return routine


# Running ------------------------------------------------------------------------------------


def is_running(rt: Runtime, rid: int) -> bool:
    return rid in ensure(rt).running


async def run_routine(
    rt: Runtime,
    routine: dict[str, Any],
    *,
    source: str = "web",
    emit: Emit | None = None,
    unattended: bool = False,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Run a saved routine's steps in order and return a summary.

    The caller has the approval: the user confirmed the routine (UI, CLI or the ``run_routine``
    tool) or scheduled it (``unattended``). ``source`` says where the run came from. A run from
    a chat passes its ``conversation_id``: the steps run in that chat's context and the summary
    is the tool's result instead of a message in the "Routines" chat.
    """
    state = ensure(rt)
    rid = routine["id"]
    if rid in state.running:
        raise RoutineError(f"“{routine['name']}” is already running.")
    state.running.add(rid)
    chat = conversation_id or conversation(rt)
    prefs, _ = rt.preferences()
    steps = routine["steps"]
    run_id = f"routine-{uuid.uuid4().hex[:8]}"
    done: list[dict[str, Any]] = []
    failed_at: int | None = None
    started = time.monotonic()
    try:
        await rt.activity.update(run_id, state="tool", conversation_id=chat, source="routine")
        for index, step in enumerate(steps):
            await rt.activity.update(run_id, tool=step["tool"])
            record = await _run_step(
                rt, routine, step, index, chat, prefs, source=source, unattended=unattended
            )
            done.append(record)
            if emit:
                await emit(
                    {"type": "routine.step", "routine_id": rid, "total": len(steps), **record}
                )
            if not record["ok"] and not step.get("continue_on_error"):
                failed_at = index
                break
    finally:
        state.running.discard(rid)
        await rt.activity.end(run_id, failed=failed_at is not None or len(done) < len(steps))
    ok = failed_at is None
    text, headline = _report(routine, done, failed_at, unattended)
    rt.store.execute(
        "UPDATE routines SET last_run = ?, last_status = ? WHERE id = ?",
        (time.time(), "ok" if ok else "failed", rid),
    )
    if conversation_id is None:
        rt.store.add_message(chat, "assistant", text, meta={"routine": rid, "ok": ok})
        await rt.broadcast({"type": "conversations.changed"})
    await rt.broadcast({"type": "routines.changed"})
    return {
        "routine_id": rid,
        "name": routine["name"],
        "ok": ok,
        "steps": done,
        "total": len(steps),
        "failed_at": failed_at,
        "conversation_id": chat,
        "headline": headline,
        "text": text,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


async def _run_step(
    rt: Runtime,
    routine: dict[str, Any],
    step: dict[str, Any],
    index: int,
    chat: str,
    prefs: Preferences,
    *,
    source: str,
    unattended: bool,
) -> dict[str, Any]:
    name = step["tool"]
    args = dict(step.get("arguments") or {})
    tool = rt.registry.get(name)
    tier = permission_for(tool, prefs) if tool else "deny"
    ran = ok = False
    started = time.monotonic()
    if tool is None:
        result = f"Not run: there is no tool named {name} any more (removed, or its server is off)."
    elif name in NOT_STEPS:
        result = "Not run: a routine can't run routines."
    elif tier == "deny":
        result = f"Not run: {name} is switched off in Settings → Tools."
    else:
        ran = True
        try:
            result, _ = await tool.run(args, rt.tool_context(chat))
            ok = True
        except ToolError as exc:
            result = f"Error: {exc}"
        except Exception as exc:
            log.exception("Routine step %s crashed", name)
            result = f"Error: {exc.__class__.__name__}: {exc}"
    duration = round((time.monotonic() - started) * 1000)
    where = f"{source}, scheduled" if unattended else source
    detail = f"routine “{routine['name']}” step {index + 1}/{len(routine['steps'])} ({where})"
    try:
        rt.store.add_audit(
            conversation_id=chat,
            source="routine",
            tool=name,
            arguments=args,
            permission=tier,
            decision="routine" if ran else "blocked",
            ok=ok if ran else None,
            duration_ms=duration if ran else None,
            detail=(detail if ok else f"{detail}: {result}")[:300],
        )
    except Exception:  # The audit log must never break a run.
        log.exception("Could not write the audit log")
    return {
        "index": index,
        "tool": name,
        "ok": ok,
        "result": result[:RESULT_CHARS],
        "note": step.get("note") or "",
        "summary": summarize(tool.summary if tool else "", name, args),
        "duration_ms": duration if ran else None,
    }


def _report(
    routine: dict[str, Any], done: list[dict[str, Any]], failed_at: int | None, unattended: bool
) -> tuple[str, str]:
    """The summary posted to the chat, and a one-line headline for notifications."""
    total = len(routine["steps"])
    passed = sum(1 for s in done if s["ok"])
    if failed_at is None:
        headline = f"{passed}/{total} steps OK"
        if passed < total:
            headline += f", {total - passed} failed (continued)"
    else:
        step = done[failed_at]
        headline = f"Stopped at step {failed_at + 1}/{total} ({step['summary']}): {step['result']}"
    mark = "[FAIL]" if failed_at is not None else "[OK]" if passed == total else "[WARN]"
    when = " · scheduled" if unattended else ""
    lines = [f"**ROUTINE // {routine['name'].upper()}** `{mark}` {passed}/{total} steps{when}", ""]
    for step in done:
        result = " ".join(step["result"].split())
        result = result if len(result) <= 160 else result[:159] + "…"
        lines.append(
            f"- `{'[OK]' if step['ok'] else '[FAIL]'}` {step['index'] + 1:02d} {step['summary']}: {result}"
        )
    for index in range(len(done), total):
        step = routine["steps"][index]
        lines.append(f"- `[SKIP]` {index + 1:02d} {step['tool']}")
    return "\n".join(lines), headline[:300]


async def run_scheduled(rt: Runtime, item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """The ``routine`` automation kind: run the routine unattended and notify with the result
    (as important when it fails)."""
    state = item.get("state") or {}
    target = str(item.get("target") or "").strip()
    routine = get_routine(rt, int(target)) if target.isdigit() else None
    chat = conversation(rt)
    if item.get("conversation_id") != chat:
        rt.store.update_automation(item["id"], conversation_id=chat)
    if routine is None:
        text = f"Routine #{target} no longer exists."
        await rt.notify(item["name"], text, conversation_id=chat, level="important")
        return "error", text, state
    title = f"ROUTINE // {routine['name'].upper()}"
    try:
        result = await run_routine(rt, routine, source="automation", unattended=True)
    except RoutineError as exc:
        await rt.notify(title, str(exc), conversation_id=chat, level="important")
        return "error", str(exc), state
    clean = result["ok"] and all(step["ok"] for step in result["steps"])
    level = "info" if clean else "important"
    await rt.notify(title, result["headline"], conversation_id=chat, level=level)
    return ("ok" if result["ok"] else "error"), result["headline"], state
