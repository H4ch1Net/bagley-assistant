"""Tools that let Bagley set reminders, schedule its own work and watch web pages."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from bagley.automations import ScheduleError
from bagley.tools import ToolContext, ToolError, tool

WHEN_HELP = "e.g. 'in 20 minutes', 'at 18:30', 'every 2 hours', 'daily at 08:00', 'weekdays at 9:00', 'mondays at 10:00'"


def _scheduler(ctx: ToolContext) -> Any:
    if ctx.runtime is None or getattr(ctx.runtime, "scheduler", None) is None:
        raise ToolError("Automations are not available here.")
    return ctx.runtime.scheduler


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "not scheduled"


def _create(ctx: ToolContext, **fields: Any) -> dict[str, Any]:
    try:
        return _scheduler(ctx).create(**fields)
    except ScheduleError as exc:
        raise ToolError(str(exc)) from exc


@tool(category="automation", summary="Remind: {message}")
async def set_reminder(
    ctx: ToolContext,
    message: Annotated[str, "What to remind the user about"],
    when: Annotated[str, WHEN_HELP],
) -> str:
    """Remind the user about something later, in this chat and as a notification. Use it whenever
    the user says "remind me"."""
    item = _create(
        ctx,
        kind="reminder",
        name=message[:60],
        prompt=message,
        schedule=when,
        conversation_id=ctx.conversation_id,
    )
    await ctx.runtime.broadcast({"type": "automations.changed"})
    return f"Reminder #{item['id']} set for {_when(item['next_run'])}."


@tool(category="automation", risk="confirm", summary="Schedule “{name}”")
async def schedule_task(
    ctx: ToolContext,
    name: Annotated[str, "Short name, e.g. 'Morning briefing'"],
    instructions: Annotated[str, "What Bagley should do each time, written as a request to itself"],
    when: Annotated[str, WHEN_HELP],
) -> str:
    """Schedule Bagley to do something by itself later or repeatedly (e.g. a morning briefing with
    the weather and news). Results land in their own chat with a notification. Unattended runs
    can't use tools that need approval. Asks the user first."""
    item = _create(ctx, kind="task", name=name, prompt=instructions, schedule=when)
    await ctx.runtime.broadcast({"type": "automations.changed"})
    return f"Scheduled “{item['name']}” (#{item['id']}). Next run: {_when(item['next_run'])}."


@tool(category="automation", risk="confirm", summary="Watch {url}")
async def watch_webpage(
    ctx: ToolContext,
    url: Annotated[str, "Page to watch (http or https)"],
    check: Annotated[
        str, "How often to check, e.g. 'every 1 hour' (at least every 5 minutes)"
    ] = "every 1 hour",
    instructions: Annotated[
        str, "Optional: what to do when it changes, e.g. 'tell me if the price drops below 300'"
    ] = "",
    name: Annotated[str, "Short name for the watcher"] = "",
) -> str:
    """Watch a web page and notify the user when its text changes. With instructions, Bagley reads
    the changes and acts on them (for example only notifying when a condition is met). Asks the
    user first."""
    label = name or url.split("://", 1)[-1][:60]
    item = _create(ctx, kind="watch", name=label, prompt=instructions, schedule=check, target=url)
    await ctx.runtime.broadcast({"type": "automations.changed"})
    return f"Watching {url} (#{item['id']}). First check: {_when(item['next_run'])}."


@tool(category="automation", summary="List automations")
def list_automations(ctx: ToolContext) -> list[dict[str, Any]]:
    """List the user's reminders, scheduled tasks and page watchers."""
    scheduler = _scheduler(ctx)
    return [
        {
            "id": a["id"],
            "kind": a["kind"],
            "name": a["name"],
            "schedule": scheduler.describe(a)["schedule_text"],
            "next_run": _when(a["next_run"]),
            "enabled": a["enabled"],
            "last_status": a["last_status"],
        }
        for a in ctx.store.list_automations()
    ]


@tool(category="automation", summary="Cancel automation #{automation_id}")
async def cancel_automation(
    ctx: ToolContext,
    automation_id: Annotated[int, "Number of the reminder, task or watcher"],
) -> str:
    """Cancel and delete a reminder, scheduled task or page watcher."""
    if not ctx.store.delete_automation(automation_id):
        raise ToolError(f"There is no automation #{automation_id}.")
    await ctx.runtime.broadcast({"type": "automations.changed"})
    return f"Cancelled #{automation_id}."
