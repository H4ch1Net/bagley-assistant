"""Endpoints for saved routines (see ``bagley.routines``).

* ``GET /api/routines`` lists them; ``POST`` saves one, which approves its exact steps.
* ``GET``, ``PATCH`` and ``DELETE /api/routines/{key}`` work on one routine by id or name.
* ``GET /api/routines/candidates?conversation_id=`` lists a chat's successful tool calls, so the
  user can tick the ones that become steps.
* ``POST /api/routines/record`` starts, stops (saves) or cancels recording in a chat;
  ``GET /api/routines/record?conversation_id=`` reports it.
* ``POST /api/routines/{key}/run`` runs a routine the user has just confirmed and streams its
  progress as NDJSON: ``routine.start``, one ``routine.step`` per step, then ``routine.end``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bagley import routines
from bagley.api import runtime
from bagley.runtime import Runtime

log = logging.getLogger("bagley.routines")
router = APIRouter()

_background: set[asyncio.Task[None]] = set()  # Runs keep going if the client goes away.


class StepIn(BaseModel):
    tool: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=200)
    continue_on_error: bool = False


class RoutineBody(BaseModel):
    name: str = Field(min_length=1, max_length=routines.MAX_NAME)
    description: str = Field(default="", max_length=500)
    steps: list[StepIn] = Field(min_length=1, max_length=routines.MAX_STEPS)


class RoutinePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=routines.MAX_NAME)
    description: str | None = Field(default=None, max_length=500)
    steps: list[StepIn] | None = Field(default=None, min_length=1, max_length=routines.MAX_STEPS)


class RecordBody(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=40)
    action: Literal["start", "stop", "cancel"]
    name: str | None = Field(default=None, max_length=routines.MAX_NAME)
    description: str = Field(default="", max_length=500)


class RunBody(BaseModel):
    source: Literal["web", "cli", "overlay", "shell", "voice", "phone", "api"] = "web"


def _steps(steps: list[StepIn]) -> list[dict[str, Any]]:
    return [s.model_dump() for s in steps]


def _find(rt: Runtime, key: str) -> dict[str, Any]:
    routine = routines.get_routine(rt, key)
    if routine is None:
        raise HTTPException(404, "Routine not found.")
    return routine


@router.get("/api/routines")
async def list_routines(request: Request) -> list[dict[str, Any]]:
    rt = runtime(request)
    prefs, _ = rt.preferences()
    return [routines.describe(rt, r, prefs) for r in routines.list_routines(rt)]


@router.post("/api/routines", status_code=201)
async def create_routine(body: RoutineBody, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    try:
        routine = routines.create_routine(rt, body.name, body.description, _steps(body.steps))
    except routines.RoutineError as exc:
        raise HTTPException(422, str(exc)) from exc
    await rt.broadcast({"type": "routines.changed"})
    return routines.describe(rt, routine)


@router.get("/api/routines/candidates")
async def candidates(conversation_id: str, request: Request) -> list[dict[str, Any]]:
    rt = runtime(request)
    if not rt.store.get_conversation(conversation_id):
        raise HTTPException(404, "Conversation not found.")
    return routines.candidates(rt.store, conversation_id, registry=rt.registry)


@router.get("/api/routines/record")
async def recording(conversation_id: str, request: Request) -> dict[str, Any]:
    return routines.recording(runtime(request), conversation_id)


@router.post("/api/routines/record")
async def record(body: RecordBody, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    cid = body.conversation_id
    try:
        if body.action == "start":
            return routines.start_recording(rt, cid)
        if body.action == "cancel":
            routines.cancel_recording(rt, cid)
            return routines.recording(rt, cid)
        if not body.name:
            raise HTTPException(422, "Name the routine to save it.")
        routine = routines.stop_recording(rt, cid, body.name, body.description)
    except routines.RoutineError as exc:
        raise HTTPException(422, str(exc)) from exc
    await rt.broadcast({"type": "routines.changed"})
    return {**routines.recording(rt, cid), "routine": routines.describe(rt, routine)}


@router.get("/api/routines/{key}")
async def get_routine(key: str, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    return routines.describe(rt, _find(rt, key))


@router.patch("/api/routines/{key}")
async def update_routine(key: str, body: RoutinePatch, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    routine = _find(rt, key)
    try:
        updated = routines.update_routine(
            rt,
            routine["id"],
            name=body.name,
            description=body.description,
            steps=_steps(body.steps) if body.steps is not None else None,
        )
    except routines.RoutineError as exc:
        raise HTTPException(422, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "Routine not found.")
    await rt.broadcast({"type": "routines.changed"})
    return routines.describe(rt, updated)


@router.delete("/api/routines/{key}", status_code=204)
async def delete_routine(key: str, request: Request) -> Response:
    rt = runtime(request)
    routines.delete_routine(rt, _find(rt, key)["id"])
    await rt.broadcast({"type": "routines.changed"})
    await rt.broadcast({"type": "automations.changed"})
    return Response(status_code=204)


@router.post("/api/routines/{key}/run")
async def run_routine(key: str, request: Request, body: RunBody | None = None) -> StreamingResponse:
    """Run a routine the user has just confirmed (the client shows the steps and asks once)."""
    rt = runtime(request)
    routine = _find(rt, key)
    if routines.is_running(rt, routine["id"]):
        raise HTTPException(409, "That routine is already running.")
    source = body.source if body else "web"
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def work() -> None:
        try:
            await emit(
                {
                    "type": "routine.start",
                    "routine_id": routine["id"],
                    "name": routine["name"],
                    "total": len(routine["steps"]),
                }
            )
            result = await routines.run_routine(rt, routine, source=source, emit=emit)
            await emit({"type": "routine.end", **result})
        except routines.RoutineError as exc:
            await emit({"type": "error", "message": str(exc)})
        except Exception as exc:
            log.exception("Routine run failed")
            await emit({"type": "error", "message": f"Something went wrong: {exc}"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(work())
    _background.add(task)
    task.add_done_callback(_background.discard)

    async def stream() -> AsyncIterator[bytes]:
        while (event := await queue.get()) is not None:
            yield (json.dumps(event, default=str) + "\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson")
