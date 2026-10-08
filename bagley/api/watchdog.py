"""Watchdog endpoints: the system report, its sections, baselines and the morning briefing.

* ``GET /api/watchdog/report?sections=a,b`` collects a report now (all sections by default).
* ``GET /api/watchdog/last`` is the most recent full report, without collecting again.
* ``GET /api/watchdog/sections`` lists the sections.
* ``POST /api/watchdog/baseline/reset`` forgets known devices, ports and peers.
* ``POST /api/watchdog/briefing`` creates the default "Morning briefing" automation if missing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from bagley.api import runtime
from bagley.tools import watchdog as _tools  # noqa: F401  Registers the briefing kind.
from bagley.watchdog import briefing
from bagley.watchdog.baseline import Baseline
from bagley.watchdog.collectors import COLLECTORS
from bagley.watchdog.report import collect, last_report, section_ids

router = APIRouter()


def _sections(value: Any) -> list[str]:
    try:
        return section_ids(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/watchdog/report")
async def report(request: Request, sections: str = "") -> dict[str, Any]:
    ids = _sections(sections or None)
    return (await collect(runtime(request), ids)).to_dict()


@router.get("/api/watchdog/last")
async def last(request: Request) -> dict[str, Any]:
    found = last_report(Baseline(runtime(request).store))
    if found is None:
        raise HTTPException(404, "No report yet.")
    return found


@router.get("/api/watchdog/sections")
async def sections() -> list[dict[str, Any]]:
    return [
        {"id": c.id, "title": c.title, "description": c.description, "baseline": c.baseline}
        for c in COLLECTORS.values()
    ]


class ResetBody(BaseModel):
    sections: list[str] | None = Field(default=None, max_length=len(COLLECTORS))


@router.post("/api/watchdog/baseline/reset")
async def reset_baseline(request: Request, body: ResetBody | None = None) -> dict[str, Any]:
    chosen = None if body is None or not body.sections else _sections(body.sections)
    removed = Baseline(runtime(request).store).reset(chosen)
    ids = chosen or list(COLLECTORS)
    return {
        "sections": [i for i in ids if COLLECTORS[i].baseline],
        "removed": len(removed),
    }


@router.post("/api/watchdog/briefing")
async def ensure_briefing(request: Request) -> dict[str, Any]:
    rt = runtime(request)
    item, created = briefing.ensure_default(rt)
    if created:
        await rt.broadcast({"type": "automations.changed"})
    return {**rt.scheduler.describe(item), "created": created}
