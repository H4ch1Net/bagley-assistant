"""Model benchmark: the latest results per machine, and runs streamed as NDJSON progress."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bagley.api import runtime
from bagley.bench import TASKS, apply_winners, latest_results, run_bench, select_tasks
from bagley.runtime import Runtime

router = APIRouter()
log = logging.getLogger("bagley.bench")


class BenchBody(BaseModel):
    machines: list[str] = Field(default_factory=list, max_length=32)  # Ids or names; [] = all.
    models: list[str] = Field(default_factory=list, max_length=32)  # Globs; [] = all chat models.
    tasks: list[str] = Field(default_factory=list, max_length=32)  # Task ids; [] = all.
    apply: bool = False  # Make each machine's winner its default model.
    detach: bool = False  # Keep running if the client goes away.


class Runs:
    """One benchmark at a time (they compete for the same GPUs), stopped on shutdown."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*self.tasks, return_exceptions=True)


def _runs(rt: Runtime) -> Runs:
    return rt.services.setdefault("bench", Runs())


@router.get("/api/bench")
async def results(request: Request) -> dict[str, Any]:
    rt = runtime(request)
    return {
        "running": _runs(rt).lock.locked(),
        "tasks": [t.public() for t in TASKS],
        "machines": latest_results(rt.store),
    }


@router.post("/api/bench/run")
async def run(body: BenchBody, request: Request) -> StreamingResponse:
    """Stream ``bench.start``, ``bench.task`` and ``bench.model`` events, then ``bench.done``
    with the full result and what ``apply`` changed, or ``error``. One JSON object a line."""
    rt = runtime(request)
    try:
        select_tasks(body.tasks)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    runs = _runs(rt)
    if runs.lock.locked():
        raise HTTPException(409, "A benchmark is already running.")
    await runs.lock.acquire()  # Free and nothing awaited since the check, so taken at once.
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def work() -> None:
        try:
            result = await run_bench(
                rt,
                machines=body.machines or None,
                models=body.models or None,
                tasks=body.tasks or None,
                progress=queue.put,
            )
            applied = apply_winners(rt, result) if body.apply else None
            await queue.put({"type": "bench.done", "result": result.to_dict(), "applied": applied})
            await rt.broadcast({"type": "bench.changed"})
        except ValueError as exc:
            await queue.put({"type": "error", "message": str(exc)})
        except Exception as exc:
            log.exception("Benchmark failed")
            await queue.put({"type": "error", "message": f"The benchmark failed: {exc}"})
        finally:
            runs.lock.release()
            queue.put_nowait(None)

    task = asyncio.create_task(work())
    runs.tasks.add(task)
    task.add_done_callback(runs.tasks.discard)

    async def stream() -> AsyncIterator[bytes]:
        finished = False
        try:
            while (event := await queue.get()) is not None:
                yield (json.dumps(event, default=str) + "\n").encode()
            finished = True
        finally:
            if not finished and not body.detach and not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")
