"""Automations: reminders, scheduled tasks and web page watchers.

They run inside the server while Bagley is running, write their results into a chat and send a
notification. Unattended runs can only use tools that don't need approval.

Schedules are short phrases, normalised when saved:

    in 20 minutes · at 18:30 · at 2026-10-04 08:00     (once)
    every 30 minutes · every 2 hours · hourly          (interval)
    daily at 08:00 · weekdays at 9:00 · weekends at 10:00 · mondays at 09:15 · mon,thu at 7:00
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.runtime import Runtime

log = logging.getLogger("bagley.automations")

KINDS = ("task", "reminder", "watch")
MIN_INTERVAL = {"task": 60, "reminder": 60, "watch": 300}
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
UNITS = {"m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600,
         "hour": 3600, "hours": 3600, "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800,
         "weeks": 604800}  # fmt: skip


class ScheduleError(ValueError):
    pass


@dataclass
class Schedule:
    kind: str  # once | interval | weekly
    at: datetime | None = None
    every: int = 0  # seconds
    days: tuple[int, ...] = ()
    hour: int = 0
    minute: int = 0

    @property
    def expr(self) -> str:
        """Normalised form that is stored and parsed again later."""
        if self.kind == "once":
            assert self.at
            return f"at {self.at.strftime('%Y-%m-%d %H:%M')}"
        if self.kind == "interval":
            return f"every {self.every // 60} minutes"
        return f"{','.join(DAY_NAMES[d] for d in self.days)} at {self.hour:02d}:{self.minute:02d}"

    def describe(self) -> str:
        if self.kind == "once":
            assert self.at
            return "Once, " + self.at.strftime("%a %d %b %H:%M")
        if self.kind == "interval":
            minutes = self.every // 60
            if minutes % 1440 == 0:
                n = minutes // 1440
                return "Every day" if n == 1 else f"Every {n} days"
            if minutes % 60 == 0:
                n = minutes // 60
                return "Every hour" if n == 1 else f"Every {n} hours"
            return f"Every {minutes} minutes"
        when = f"{self.hour:02d}:{self.minute:02d}"
        if len(self.days) == 7:
            return f"Every day at {when}"
        if self.days == (0, 1, 2, 3, 4):
            return f"Weekdays at {when}"
        if self.days == (5, 6):
            return f"Weekends at {when}"
        if len(self.days) == 1:
            return f"{DAY_FULL[self.days[0]]}s at {when}"
        names = ", ".join(DAY_NAMES[d].capitalize() for d in self.days)
        return f"{names} at {when}"

    def next_after(self, now: datetime, anchor: datetime | None = None) -> datetime | None:
        if self.kind == "once":
            return self.at if self.at and self.at > now else None
        if self.kind == "interval":
            start = anchor or now
            if start > now:
                return start
            periods = int((now - start).total_seconds() // self.every) + 1
            return start + timedelta(seconds=periods * self.every)
        candidate = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        for offset in range(8):
            day = candidate + timedelta(days=offset)
            if day.weekday() in self.days and day > now:
                return day
        return None


def _clock(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text.strip())
    if not m:
        raise ScheduleError(f"Couldn't read the time '{text}'. Use 24h HH:MM, e.g. 08:30.")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        if not 1 <= hour <= 12:
            raise ScheduleError(f"'{text}' is not a valid time.")
        hour = hour % 12 + (12 if m.group(3) == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ScheduleError(f"'{text}' is not a valid time.")
    return hour, minute


def parse_schedule(text: str, now: datetime | None = None) -> Schedule:
    now = now or datetime.now().astimezone()
    s = " ".join(text.lower().strip().split())
    s = re.sub(r"^(?:every ?day|everyday)\b", "daily", s)
    if not s:
        raise ScheduleError("The schedule is empty.")

    if m := re.fullmatch(r"in (\d+) ?([a-z]+)", s):
        unit = UNITS.get(m.group(2))
        if not unit:
            raise ScheduleError(f"Unknown unit '{m.group(2)}'. Use minutes, hours or days.")
        return Schedule(
            "once",
            at=(now + timedelta(seconds=int(m.group(1)) * unit)).replace(second=0, microsecond=0),
        )
    if m := re.fullmatch(r"(?:at|on) (\d{4}-\d{2}-\d{2})[ t](\d{1,2}:\d{2})", s):
        try:
            at = datetime.fromisoformat(f"{m.group(1)} {m.group(2).zfill(5)}")
        except ValueError as exc:
            raise ScheduleError(f"'{text}' is not a valid date.") from exc
        return Schedule("once", at=at.replace(tzinfo=now.tzinfo))
    if m := re.fullmatch(r"(?:at )?(\d{1,2}(?::\d{2})? ?(?:am|pm)?)(?: today| tomorrow)?", s):
        hour, minute = _clock(m.group(1))
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if s.endswith("tomorrow") or at <= now:
            at += timedelta(days=1)
        return Schedule("once", at=at)
    if s in ("hourly", "every hour"):
        return Schedule("interval", every=3600)
    if m := re.fullmatch(r"every (\d+) ?([a-z]+)", s):
        unit = UNITS.get(m.group(2))
        if not unit:
            raise ScheduleError(f"Unknown unit '{m.group(2)}'. Use minutes, hours or days.")
        return Schedule("interval", every=int(m.group(1)) * unit)
    if m := re.fullmatch(r"(?:every )?(daily|weekdays|weekends|[a-z, ]+?) at (.+)", s):
        days_text = m.group(1)
        if days_text == "daily":
            days = tuple(range(7))
        elif days_text == "weekdays":
            days = (0, 1, 2, 3, 4)
        elif days_text == "weekends":
            days = (5, 6)
        else:
            found = set()
            for part in re.split(r"[ ,]+|and", days_text):
                part = part.strip().rstrip("s")
                if not part:
                    continue
                idx = next((i for i, d in enumerate(DAY_NAMES) if part.startswith(d)), None)
                if idx is None:
                    raise ScheduleError(f"Unknown day '{part}'.")
                found.add(idx)
            days = tuple(sorted(found))
        hour, minute = _clock(m.group(2))
        return Schedule("weekly", days=days, hour=hour, minute=minute)
    raise ScheduleError(
        f"Couldn't understand '{text}'. Try 'in 20 minutes', 'at 18:30', 'every 2 hours', "
        "'daily at 08:00', 'weekdays at 9:00' or 'mondays at 10:00'."
    )


def preview(text: str, kind: str = "task") -> dict[str, Any]:
    """Validate a schedule for the UI and the tools."""
    sched = parse_schedule(text)
    if sched.kind == "interval" and sched.every < MIN_INTERVAL.get(kind, 60):
        raise ScheduleError(
            f"The shortest interval for this is {MIN_INTERVAL[kind] // 60} minutes."
        )
    now = datetime.now().astimezone()
    nxt = sched.next_after(now, now if sched.kind == "interval" else None)
    if nxt is None:
        raise ScheduleError("That time is in the past.")
    return {"schedule": sched.expr, "description": sched.describe(), "next_run": nxt.timestamp()}


class Scheduler:
    """Runs due automations. One at a time, checked every few seconds."""

    def __init__(self, runtime: Runtime, interval: float = 5.0) -> None:
        self.rt = runtime
        self.interval = interval
        self.running: set[int] = set()
        self._task: asyncio.Task[None] | None = None

    # Lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Automation tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self, now: float | None = None) -> int:
        due = self.rt.store.due_automations(now or time.time())
        for item in due:
            await self.run(item, manual=False)
        return len(due)

    # CRUD ---------------------------------------------------------------------------------

    def create(
        self,
        kind: str,
        name: str,
        schedule: str,
        prompt: str = "",
        target: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise ScheduleError(f"Unknown kind '{kind}'.")
        if kind == "watch" and not (target or "").startswith(("http://", "https://")):
            raise ScheduleError("A watcher needs an http(s) URL.")
        if kind in ("task", "reminder") and not (prompt or name).strip():
            raise ScheduleError("Say what the automation should do.")
        info = preview(schedule, kind)
        return self.rt.store.create_automation(
            kind=kind,
            name=" ".join(name.split())[:80] or kind.title(),
            prompt=prompt.strip()[:4000],
            schedule=info["schedule"],
            target=target,
            conversation_id=conversation_id,
            next_run=info["next_run"],
        )

    def update(self, aid: int, **changes: Any) -> dict[str, Any] | None:
        item = self.rt.store.get_automation(aid)
        if not item:
            return None
        if "schedule" in changes:
            info = preview(changes["schedule"], item["kind"])
            changes["schedule"] = info["schedule"]
            changes["next_run"] = info["next_run"]
        if changes.get("enabled") and not item["enabled"] and "next_run" not in changes:
            info = preview(item["schedule"], item["kind"])
            changes["next_run"] = info["next_run"]
        return self.rt.store.update_automation(aid, **changes)

    def describe(self, item: dict[str, Any]) -> dict[str, Any]:
        try:
            text = parse_schedule(item["schedule"]).describe()
        except ScheduleError:
            text = item["schedule"]
        return {**item, "schedule_text": text, "running": item["id"] in self.running}

    # Running ------------------------------------------------------------------------------

    async def run(self, item: dict[str, Any], *, manual: bool = True) -> None:
        """Run one automation now. ``manual`` (Run now) leaves a one-time schedule in place."""
        aid = item["id"]
        if aid in self.running:
            return
        self.running.add(aid)
        store = self.rt.store
        await self.rt.broadcast({"type": "automations.changed"})
        status, result, state = "ok", "", item.get("state") or {}
        try:
            if item["kind"] == "reminder":
                result = await self._remind(item)
            elif item["kind"] == "watch":
                status, result, state = await self._watch(item)
            else:
                status, result = await self._task_run(item)
        except Exception as exc:
            log.exception("Automation %s failed", aid)
            status, result = "error", str(exc) or exc.__class__.__name__
        finally:
            self.running.discard(aid)
        now = datetime.now().astimezone()
        try:
            sched = parse_schedule(item["schedule"])
            anchor = (
                datetime.fromtimestamp(item["next_run"]).astimezone()
                if item.get("next_run")
                else now
            )
            if sched.kind == "once" and not manual:
                nxt = None  # A one-time automation that ran on schedule is done.
            else:
                nxt = sched.next_after(now, anchor if sched.kind == "interval" else None)
        except ScheduleError:
            nxt = None
        store.update_automation(
            aid,
            last_run=time.time(),
            last_status=status,
            last_result=_snippet(result, 300),
            state=state,
            next_run=nxt.timestamp() if nxt else None,
            enabled=bool(nxt) and item["enabled"],
        )
        await self.rt.broadcast({"type": "automations.changed"})
        await self.rt.broadcast({"type": "conversations.changed"})

    def _conversation(self, item: dict[str, Any]) -> str:
        store = self.rt.store
        cid = item.get("conversation_id")
        if cid and store.get_conversation(cid):
            return cid
        conv = store.create_conversation(item["name"])
        store.update_automation(item["id"], conversation_id=conv["id"])
        return conv["id"]

    async def _remind(self, item: dict[str, Any]) -> str:
        cid = self._conversation(item)
        text = item["prompt"] or item["name"]
        self.rt.store.add_message(
            cid, "assistant", f"⏰ **Reminder:** {text}", meta={"automation": item["id"]}
        )
        await self.rt.notify("Reminder", text, conversation_id=cid)
        return text

    async def _agent(self, item: dict[str, Any], prompt: str) -> tuple[str, str]:
        """Run the agent unattended in the automation's chat. Returns (status, final text)."""
        from bagley.agent import Agent, RunRequest  # Imported late: agent imports runtime.

        cid = self._conversation(item)
        events: list[dict[str, Any]] = []

        async def emit(event: dict[str, Any]) -> None:
            events.append(event)

        async def deny(call: Any, tool: Any) -> bool:
            return False  # Nobody is there to approve; risky tools are declined.

        await Agent(self.rt).run(RunRequest(text=prompt, conversation_id=cid), emit, deny)
        errors = [e for e in events if e["type"] == "error"]
        text = "".join(e["text"] for e in events if e["type"] == "text.delta").strip()
        if errors and not text:
            await self.rt.notify(
                item["name"], errors[0]["message"], conversation_id=cid, level="error"
            )
            return "error", errors[0]["message"]
        await self.rt.notify(item["name"], _snippet(text), conversation_id=cid)
        return "ok", text

    async def _task_run(self, item: dict[str, Any]) -> tuple[str, str]:
        when = datetime.now().strftime("%a %d %b %H:%M")
        return await self._agent(
            item, f"[Scheduled task “{item['name']}”, {when}] {item['prompt']}"
        )

    async def _watch(self, item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        from bagley.tools.web import fetch_limited, html_to_text

        ctx = self.rt.tool_context()
        url = item["target"]
        _, resp = await fetch_limited(ctx, url)
        if resp.status_code >= 400:
            return "error", f"{url} returned HTTP {resp.status_code}.", item.get("state") or {}
        ctype = resp.headers.get("content-type", "")
        text = (
            html_to_text(resp.text)[1]
            if "html" in ctype or resp.text.lstrip().startswith("<")
            else resp.text
        )
        text = text[:60_000]
        digest = hashlib.sha256(text.encode()).hexdigest()
        state = item.get("state") or {}
        if not state.get("hash"):
            return "ok", "Started watching.", {"hash": digest, "text": text}
        if state["hash"] == digest:
            return "no change", "No change.", state
        diff = "\n".join(
            line
            for line in difflib.unified_diff(
                state.get("text", "").splitlines(), text.splitlines(), lineterm="", n=0
            )
            if not line.startswith(("---", "+++", "@@"))
        )[:6000]
        new_state = {"hash": digest, "text": text}
        if item["prompt"]:
            prompt = (
                f"[Watcher “{item['name']}”] The page {url} changed. Changed lines "
                f"(+ added, - removed):\n{diff}\n\nInstructions: {item['prompt']}"
            )
            status, result = await self._agent(item, prompt)
            return status, result, new_state
        cid = self._conversation(item)
        added = sum(1 for line in diff.splitlines() if line.startswith("+"))
        removed = sum(1 for line in diff.splitlines() if line.startswith("-"))
        summary = f"{url} changed: {added} lines added, {removed} removed."
        self.rt.store.add_message(
            cid,
            "assistant",
            f"👁 **{item['name']}**: {summary}\n\n```diff\n{diff[:3000]}\n```",
            meta={"automation": item["id"]},
        )
        await self.rt.notify(item["name"], summary, conversation_id=cid)
        return "changed", summary, new_state


def _snippet(text: str, limit: int = 160) -> str:
    plain = re.sub(r"[*_`#>|]", "", " ".join(text.split()))
    return plain if len(plain) <= limit else plain[: limit - 1].rsplit(" ", 1)[0] + "…"
