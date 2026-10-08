"""Export, import and the always-on runner.

* ``GET /api/export?parts=automations,memories,routines`` and ``POST /api/import`` move data
  between machines as JSON.
* ``GET /api/runner`` checks the runner at ``prefs.runner_url``.
* ``POST /api/automations/{id}/move-to-runner`` and ``POST /api/runner/push`` move automations
  there; ``POST /api/runner/pull`` imports the runner's automations here, disabled.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from bagley import runner
from bagley.api import runtime

router = APIRouter()

MAX_IMPORT_BYTES = 20_000_000


class PushBody(BaseModel):
    all: bool = False
    ids: list[int] = Field(default_factory=list, max_length=1000)


def _raise(exc: runner.RunnerError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.get("/api/export")
async def export(request: Request, parts: str = ",".join(runner.PARTS)) -> dict[str, Any]:
    names = [p.strip() for p in parts.split(",") if p.strip()]
    try:
        return runner.export_data(runtime(request), names)
    except runner.DataError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/import")
async def import_(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "That export is too large.")
    try:
        data = json.loads(bytes(raw))
    except ValueError as exc:
        raise HTTPException(400, "Send the export as JSON.") from exc
    rt = runtime(request)
    try:
        result = runner.import_data(rt, data)
    except runner.DataError as exc:
        raise HTTPException(422, str(exc)) from exc
    await rt.broadcast({"type": "automations.changed"})
    return result


@router.get("/api/runner")
async def runner_status(request: Request) -> dict[str, Any]:
    return await runner.probe(runtime(request))


@router.post("/api/automations/{aid}/move-to-runner")
async def move_to_runner(aid: int, request: Request) -> dict[str, Any]:
    try:
        return await runner.move(runtime(request), aid)
    except runner.RunnerError as exc:
        raise _raise(exc) from exc


@router.post("/api/runner/push")
async def push(body: PushBody, request: Request) -> dict[str, Any]:
    if not body.all and not body.ids:
        raise HTTPException(422, "Pass all: true or the automation ids.")
    try:
        return await runner.push(runtime(request), None if body.all else body.ids)
    except runner.RunnerError as exc:
        raise _raise(exc) from exc


@router.post("/api/runner/pull")
async def pull(request: Request) -> dict[str, Any]:
    try:
        return await runner.pull(runtime(request))
    except runner.RunnerError as exc:
        raise _raise(exc) from exc
