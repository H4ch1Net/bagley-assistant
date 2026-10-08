"""The user's own activity by date, the sources it comes from, and the weekly recap automation.

* ``GET /api/life/activity?period=tuesday`` returns ``bagley.life.activity`` for the period.
* ``GET /api/life/sources`` lists the vaults and the git repositories found in code folders.
* ``POST /api/life/recap-automation`` creates the "Weekly recap" automation unless one exists.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from bagley import life
from bagley.api import runtime
from bagley.automations import ScheduleError
from bagley.tools.life import TOOL_BUDGET  # Also registers the recap automation kind.

router = APIRouter()

RECAP_NAME = "Weekly recap"
RECAP_SCHEDULE = "fridays at 17:00"


@router.get("/api/life/activity")
async def activity(
    request: Request, period: str = "today", compact: bool = False
) -> dict[str, Any]:
    """``compact`` trims the lists to what fits in a model prompt (``bagley recap --write``)."""
    try:
        start, end, label = life.parse_period(period[:100])
    except life.PeriodError as exc:
        raise HTTPException(422, str(exc)) from exc
    budget = TOOL_BUDGET if compact else None
    return await life.activity(runtime(request), start, end, label=label, budget=budget)


@router.get("/api/life/sources")
async def sources(request: Request) -> dict[str, Any]:
    prefs, _ = runtime(request).preferences()
    return await asyncio.to_thread(life.sources, prefs.code_folders, prefs.vaults)


@router.post("/api/life/recap-automation")
async def recap_automation(request: Request) -> dict[str, Any]:
    rt = runtime(request)
    existing = next((a for a in rt.store.list_automations() if a["kind"] == "recap"), None)
    if existing:
        return {"created": False, "automation": rt.scheduler.describe(existing)}
    try:
        item = rt.scheduler.create("recap", RECAP_NAME, RECAP_SCHEDULE)
    except ScheduleError as exc:
        raise HTTPException(422, str(exc)) from exc
    await rt.broadcast({"type": "automations.changed"})
    return {"created": True, "automation": rt.scheduler.describe(item)}
