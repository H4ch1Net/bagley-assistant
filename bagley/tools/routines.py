"""Tools for saved routines: list them, run one, save a new one.

A routine is a named, ordered list of tool calls with fixed arguments that the user approved
once (see ``bagley.routines``). This module also registers the ``routine`` automation kind,
which runs a routine on a schedule with nobody watching.
"""

from __future__ import annotations

from typing import Annotated, Any

from bagley import routines
from bagley.automations import Kind, ScheduleError, register_kind
from bagley.policy import reads_private
from bagley.toolroute import register_group
from bagley.tools import ToolContext, ToolError, tool

register_group(
    "routines",
    "saved routines (named sequences of tool calls the user approved) and desktop scenes",
    r"\b(routine|macro|scene|mode\b|set ?up|again|usual)",
)
reads_private("list_routines")


def _runtime(ctx: ToolContext) -> Any:
    if ctx.runtime is None:
        raise ToolError("Routines are not available here.")
    return ctx.runtime


@tool(category="routines", summary="List routines")
def list_routines(ctx: ToolContext) -> list[dict[str, Any]]:
    """List the user's saved routines (named sequences of tool calls they approved once) with
    their steps, and how their last run went."""
    rt = _runtime(ctx)
    prefs, _ = rt.preferences()
    return [
        {
            "name": r["name"],
            "description": r["description"],
            "steps": [s["summary"] for s in routines.describe(rt, r, prefs)["steps"]],
            "last_status": r["last_status"],
        }
        for r in routines.list_routines(rt)
    ]


@tool(category="routines", risk="confirm", summary="Run routine {name}", timeout=300)
async def run_routine(ctx: ToolContext, name: Annotated[str, "Name of the saved routine"]) -> str:
    """Run one of the user's saved routines: its approved steps run in order, e.g. "do my usual
    setup" or "run that again". Asks the user once for the whole routine."""
    rt = _runtime(ctx)
    routine = routines.get_routine(rt, name) or routines.find_scene(rt, name)
    if routine is None:
        names = ", ".join(r["name"] for r in routines.list_routines(rt)) or "none"
        raise ToolError(f"There is no routine called '{name}'. Saved routines: {names}.")
    try:
        result = await routines.run_routine(
            rt, routine, source="chat", conversation_id=ctx.conversation_id
        )
    except routines.RoutineError as exc:
        raise ToolError(str(exc)) from exc
    if not result["ok"]:
        raise ToolError(result["text"])
    return result["text"]


@tool(category="routines", risk="confirm", summary="Save routine “{name}”")
async def save_routine(
    ctx: ToolContext,
    name: Annotated[str, "Short name, e.g. 'coding' or 'morning'"],
    steps: Annotated[
        list[dict], "Tool calls in order: {tool, arguments, note}; arguments are fixed"
    ],
    description: Annotated[str, "One sentence on what it does"] = "",
    replace: Annotated[bool, "Replace a routine that has the same name"] = False,
) -> str:
    """Save a routine: an ordered list of tool calls with fixed arguments that the user can run
    again later with one approval (offer it after doing something they will want to repeat).
    Asks the user first; approving saves exactly these steps."""
    rt = _runtime(ctx)
    try:
        existing = routines.get_routine(rt, routines.clean_name(name))
        if existing and replace:
            routine = routines.update_routine(
                rt, existing["id"], description=description, steps=steps
            )
        else:
            routine = routines.create_routine(rt, name, description, steps)
    except routines.RoutineError as exc:
        raise ToolError(str(exc)) from exc
    assert routine is not None
    await rt.broadcast({"type": "routines.changed"})
    count = len(routine["steps"])
    return f"Saved routine “{routine['name']}” (#{routine['id']}) with {count} step(s)."


save_routine.parameters["properties"]["steps"]["items"] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "description": "Tool name"},
        "arguments": {"type": "object", "description": "The tool's arguments, fixed"},
        "note": {"type": "string", "description": "Optional note for the user"},
    },
    "required": ["tool", "arguments"],
}


# The automation kind ------------------------------------------------------------------------


def _check_target(fields: dict[str, Any]) -> None:
    target = str(fields.get("target") or "").strip()
    if not target.isdigit():
        raise ScheduleError("Choose the routine to run (its number).")
    if routines.known_routine(int(target)) is False:
        raise ScheduleError(f"There is no routine #{target}.")


async def _run_scheduled(scheduler: Any, item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return await routines.run_scheduled(scheduler.rt, item)


register_kind(Kind(name="routine", run=_run_scheduled, validate=_check_target))
