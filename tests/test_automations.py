from __future__ import annotations

import time
from datetime import datetime

import pytest

from bagley.automations import ScheduleError, parse_schedule, preview
from tests.mock_llm import WATCHED_PAGE, Reply

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 3, 14, 5).astimezone()  # A Saturday.


@pytest.mark.parametrize(
    ("text", "expr", "description", "next_run"),
    [
        ("in 20 minutes", "at 2026-10-03 14:25", "Once, Sat 03 Oct 14:25", "2026-10-03 14:25"),
        ("at 9am", "at 2026-10-04 09:00", "Once, Sun 04 Oct 09:00", "2026-10-04 09:00"),
        ("every 2 hours", "every 120 minutes", "Every 2 hours", "2026-10-03 16:05"),
        (
            "every day at 7:30",
            "mon,tue,wed,thu,fri,sat,sun at 07:30",
            "Every day at 07:30",
            "2026-10-04 07:30",
        ),
        (
            "weekdays at 9:00",
            "mon,tue,wed,thu,fri at 09:00",
            "Weekdays at 09:00",
            "2026-10-05 09:00",
        ),
        ("every monday at 6pm", "mon at 18:00", "Mondays at 18:00", "2026-10-05 18:00"),
    ],
)
def test_parse_schedule(text, expr, description, next_run):
    sched = parse_schedule(text, NOW)
    assert sched.expr == expr
    assert sched.describe() == description
    nxt = sched.next_after(NOW, NOW if sched.kind == "interval" else None)
    assert nxt.strftime("%Y-%m-%d %H:%M") == next_run
    assert parse_schedule(sched.expr, NOW).expr == expr  # Normalised form round-trips.


@pytest.mark.parametrize(
    "text", ["sometime", "every 3 fortnights", "at 25:00", "funday at 9:00", ""]
)
def test_bad_schedules(text):
    with pytest.raises(ScheduleError):
        parse_schedule(text, NOW)


def test_preview_limits():
    with pytest.raises(ScheduleError, match="shortest interval"):
        preview("every 2 minutes", "watch")
    with pytest.raises(ScheduleError, match="in the past"):
        preview("at 2001-01-01 10:00")
    assert preview("every 5 minutes", "watch")["description"] == "Every 5 minutes"


def due_now(rt, item):
    rt.store.update_automation(item["id"], next_run=time.time() - 1)


async def test_reminder_runs_once_and_notifies(make_runtime):
    rt = make_runtime()
    seen = []

    async def listener(event):
        seen.append(event)

    rt.listeners.add(listener)
    chat = rt.store.create_conversation("Errands")
    item = rt.scheduler.create(
        "reminder", "Milk", "in 5 minutes", prompt="Buy milk", conversation_id=chat["id"]
    )
    assert await rt.scheduler.tick() == 0  # Not due yet.
    due_now(rt, item)
    assert await rt.scheduler.tick() == 1
    assert rt.store.list_messages(chat["id"])[-1]["content"] == "⏰ **Reminder:** Buy milk"
    note = next(e for e in seen if e["type"] == "notification")
    assert note == {
        "type": "notification",
        "title": "Reminder",
        "body": "Buy milk",
        "conversation_id": chat["id"],
        "level": "info",
    }
    done = rt.store.get_automation(item["id"])
    assert done["enabled"] is False and done["next_run"] is None and done["last_status"] == "ok"


async def test_scheduled_task_runs_agent_unattended(make_runtime, mock):
    rt = make_runtime()
    item = rt.scheduler.create(
        "task", "Lisbon weather", "every 1 day", prompt="What's the weather in Lisbon?"
    )
    first_next = item["next_run"]
    due_now(rt, item)
    await rt.scheduler.tick()
    done = rt.store.get_automation(item["id"])
    assert done["last_status"] == "ok" and "22°C" in done["last_result"]
    assert done["enabled"] and done["next_run"] >= first_next - 1  # Rescheduled, not drifting.
    messages = rt.store.list_messages(done["conversation_id"])
    assert messages[0]["content"].startswith("[Scheduled task “Lisbon weather”")
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert rt.store.get_conversation(done["conversation_id"])["unread"] == 1


async def test_unattended_runs_decline_risky_tools(make_runtime, mock):
    rt = make_runtime()
    mock.script = [
        Reply(tool_calls=[("write_file", {"path": "x.txt", "content": "hi"})]),
        Reply(text="Couldn't."),
    ]
    item = rt.scheduler.create("task", "Write", "in 1 minute", prompt="write a file")
    due_now(rt, item)
    await rt.scheduler.tick()
    assert not (rt.config.workspace / "x.txt").exists()


async def test_watcher_detects_changes(make_runtime):
    rt = make_runtime()
    item = rt.scheduler.create(
        "watch", "Laptop price", "every 5 minutes", target="http://93.184.215.14/watched"
    )
    due_now(rt, item)
    await rt.scheduler.tick()
    assert rt.store.get_automation(item["id"])["last_result"] == "Started watching."

    due_now(rt, item)
    await rt.scheduler.tick()
    assert rt.store.get_automation(item["id"])["last_status"] == "no change"

    WATCHED_PAGE["html"] = "<html><body><h1>Prices</h1><p>Laptop: 849</p></body></html>"
    try:
        due_now(rt, item)
        await rt.scheduler.tick()
    finally:
        WATCHED_PAGE["html"] = "<html><body><h1>Prices</h1><p>Laptop: 999</p></body></html>"
    done = rt.store.get_automation(item["id"])
    assert done["last_status"] == "changed"
    message = rt.store.list_messages(done["conversation_id"])[-1]["content"]
    assert "-Laptop: 999" in message and "+Laptop: 849" in message


async def test_reminder_tool_from_chat(make_runtime, mock, recorder):
    from bagley.agent import Agent, RunRequest

    rt = make_runtime()
    mock.script = [
        Reply(tool_calls=[("set_reminder", {"message": "Call mum", "when": "at 18:30"})]),
        Reply(text="Will do."),
    ]
    await Agent(rt).run(
        RunRequest(text="remind me to call mum at 18:30"), recorder.emit, recorder.approve
    )
    cid = recorder.of("conversation")[0]["conversation"]["id"]
    reminder = rt.store.list_automations()[0]
    assert reminder["kind"] == "reminder" and reminder["conversation_id"] == cid
    assert "Reminder #1 set for" in recorder.of("tool.end")[0]["result"]
    assert recorder.asked == []  # Reminders don't need approval; scheduled tasks do.
    assert rt.registry.get("schedule_task").risk == "confirm"
