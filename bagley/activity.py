"""What Bagley is doing right now, across every run, for the desktop bar and other windows.

Each agent run reports its state here. The snapshot shows the most urgent state of all runs
(an approval beats a running tool beats thinking), the machine answering and the readout code
the avatar uses (IDLE, THINK, EXEC, AWAIT...). Clients follow it with ``GET /api/activity``
or the NDJSON stream at ``/api/activity/stream``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.runtime import Runtime

CODES = {
    "idle": "IDLE",
    "listening": "INPUT",
    "thinking": "THINK",
    "reasoning": "REASON",
    "writing": "TX",
    "tool": "EXEC",
    "approval": "AWAIT",
    "speaking": "VOICE",
    "happy": "DONE",
    "error": "ERROR",
    "offline": "NO SIGNAL",
}
PRIORITY = ["approval", "tool", "reasoning", "thinking", "writing", "speaking", "listening"]
SETTLE = {"happy": 2.0, "error": 4.0}  # Seconds a finished run's state stays visible.


@dataclass
class RunState:
    run_id: str
    state: str = "thinking"
    tool: str = ""
    conversation_id: str | None = None
    source: str = "web"
    machine: str = ""
    model: str = ""
    started: float = field(default_factory=time.time)


class Activity:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self.runs: dict[str, RunState] = {}
        self.last: dict[str, Any] = {"state": "idle", "until": 0.0, "machine": "", "model": ""}
        self.speaking = False
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._snapshot: dict[str, Any] = {}
        self._settle: asyncio.TimerHandle | None = None

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        runs = list(self.runs.values())
        state = "idle"
        tool = ""
        current = None
        for name in PRIORITY:
            match = next((r for r in runs if r.state == name), None)
            if match:
                state, tool, current = name, match.tool, match
                break
        if current is None and runs:
            current = runs[-1]
            state = current.state
        if state == "idle" and self.speaking:
            state = "speaking"
        if state == "idle" and self.last["until"] > now:
            state = self.last["state"]
        machine = current.machine if current else self.last.get("machine", "")
        model = current.model if current else self.last.get("model", "")
        return {
            "state": state,
            "code": CODES.get(state, state.upper()),
            "tool": tool,
            "runs": len(runs),
            "approvals": len(self.rt.approvals.items),
            "machine": machine,
            "model": model,
            "conversation_id": current.conversation_id if current else None,
            "source": current.source if current else "",
        }

    async def update(self, run_id: str, **fields: Any) -> None:
        run = self.runs.get(run_id)
        if run is None:
            run = self.runs[run_id] = RunState(run_id)
        for key, value in fields.items():
            setattr(run, key, value)
        await self._publish()

    async def end(self, run_id: str, *, failed: bool = False, stopped: bool = False) -> None:
        run = self.runs.pop(run_id, None)
        if run is not None and not stopped:
            state = "error" if failed else "happy"
            self.last = {
                "state": state,
                "until": time.time() + SETTLE[state],
                "machine": run.machine,
                "model": run.model,
            }
            self._schedule_settle(SETTLE[state])
        await self._publish()

    async def set_speaking(self, speaking: bool) -> None:
        self.speaking = speaking
        await self._publish()

    def _schedule_settle(self, delay: float) -> None:
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            if self._settle:
                self._settle.cancel()
            self._settle = loop.call_later(delay + 0.05, lambda: loop.create_task(self._publish()))

    async def _publish(self) -> None:
        snap = self.snapshot()
        if snap == self._snapshot:
            return
        self._snapshot = snap
        for queue in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(snap)
        await self.rt.broadcast({"type": "activity", **snap})

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)
