from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from bagley import routines
from bagley.agent import Agent, RunRequest
from bagley.automations import ScheduleError
from bagley.cli import main
from bagley.client import Client
from bagley.commands import routines as routine_cli
from bagley.config import ServerConfig
from bagley.routines import RoutineError
from bagley.runtime import Runtime
from bagley.server import create_app
from bagley.tools import ToolError, tool
from bagley.tools import desktop_control as dc
from tests.demo_stack import free_port
from tests.mock_llm import Reply
from tests.test_desktop_control import FakeRunner

pytestmark = pytest.mark.anyio

FLIPS: list[tuple[str, int]] = []


@tool(category="test", risk="confirm", summary="Flip {name}")
def flip(name: str, times: int = 1) -> str:
    """Flip a switch."""
    FLIPS.append((name, times))
    return f"Flipped {name} {times}x."


@tool(category="test", summary="Explode")
def explode() -> str:
    """Always fails."""
    raise ToolError("boom")


@pytest.fixture
def rt(make_runtime):
    FLIPS.clear()
    runtime = make_runtime()
    runtime.registry.add(flip)
    runtime.registry.add(explode)
    if runtime.registry.get("launch_app") is None:  # Desktop tools register on Linux only.
        runtime.registry.add_module(dc, "builtin")
    return runtime


@pytest.fixture
def desktop(monkeypatch):
    runner = FakeRunner()
    env = {"HYPRLAND_INSTANCE_SIGNATURE": "abc123"}
    monkeypatch.setattr(dc, "desktop", dc.Desktop(runner=runner, which=lambda n: n, env=env))
    return runner


def steps(*calls: tuple[str, dict]) -> list[dict]:
    return [{"tool": name, "arguments": args} for name, args in calls]


def test_crud_and_validation(rt):
    lights = routines.create_routine(
        rt, "  Desk   lights ", "Both lamps", steps(("flip", {"name": "desk", "times": "2"}))
    )
    assert lights["name"] == "Desk lights" and lights["builtin"] is False
    assert lights["steps"] == [
        {"tool": "flip", "arguments": {"name": "desk", "times": 2}, "note": ""}
    ]
    assert routines.get_routine(rt, "desk LIGHTS")["id"] == lights["id"]
    assert routines.get_routine(rt, str(lights["id"]))["name"] == "Desk lights"
    assert routines.get_routine(rt, "nothing") is None

    bad = {
        "already exists": ("desk lights", steps(("flip", {"name": "x"}))),
        "no tool named 'shred'": ("a", steps(("shred", {}))),
        "Missing required argument": ("b", steps(("flip", {"times": 1}))),
        "should be of type integer": ("c", steps(("flip", {"name": "x", "times": "lots"}))),
        "can't use run_routine": ("d", steps(("run_routine", {"name": "desk lights"}))),
        "at least one step": ("e", []),
        "up to 20 steps": ("f", steps(*[("flip", {"name": "x"})] * 21)),
        "needs at least one letter": ("123", steps(("flip", {"name": "x"}))),
        "Give the routine a name": ("  ", steps(("flip", {"name": "x"}))),
    }
    for message, (name, items) in bad.items():
        with pytest.raises(RoutineError, match=message):
            routines.create_routine(rt, name, "", items)

    other = routines.create_routine(rt, "Alarm", "", steps(("flip", {"name": "bell"})))
    with pytest.raises(RoutineError, match="already exists"):
        routines.update_routine(rt, other["id"], name="DESK LIGHTS")
    moved = routines.update_routine(
        rt,
        other["id"],
        name="Bell",
        steps=[{"tool": "flip", "arguments": {"name": "bell"}, "continue_on_error": True}],
    )
    assert moved["name"] == "Bell" and moved["steps"][0]["continue_on_error"] is True
    names = [r["name"] for r in routines.list_routines(rt)]
    assert names.index("Bell") < names.index("Desk lights")
    assert routines.delete_routine(rt, other["id"]) is True
    assert routines.delete_routine(rt, other["id"]) is False
    assert routines.get_routine(rt, "Bell") is None


def test_coding_scene_is_seeded_once(rt, make_runtime):
    coding = routines.get_routine(rt, "coding")
    assert coding["builtin"] is True
    assert [(s["tool"], s["arguments"]) for s in coding["steps"]] == [
        ("launch_app", {"command": "kitty", "workspace": 2}),
        ("launch_app", {"command": "zed", "workspace": 2}),
        ("launch_app", {"command": "lazygit", "workspace": 2}),
        ("switch_workspace", {"workspace": 2}),
    ]
    assert routines.find_scene(rt, "Coding mode")["id"] == coding["id"]
    assert routines.find_scene(rt, "the coding scene")["id"] == coding["id"]
    routines.delete_routine(rt, coding["id"])
    again = make_runtime()  # Same database: a deleted scene stays deleted.
    again.registry.add_module(dc, "builtin")
    assert routines.get_routine(again, "coding") is None


def test_no_scene_without_desktop_tools(make_runtime):
    rt = make_runtime()
    for name in [n for n, t in rt.registry.tools.items() if t.category == "desktop"]:
        del rt.registry.tools[name]
    assert routines.list_routines(rt) == []


async def test_run_stops_at_the_first_failure(rt):
    events = []

    async def emit(event):
        events.append(event)

    routine = routines.create_routine(
        rt,
        "Lights",
        "",
        steps(("flip", {"name": "desk"}), ("explode", {}), ("flip", {"name": "hall"})),
    )
    result = await routines.run_routine(rt, routine, source="web", emit=emit)
    assert result["ok"] is False and result["failed_at"] == 1 and result["total"] == 3
    assert [s["ok"] for s in result["steps"]] == [True, False]
    assert FLIPS == [("desk", 1)]
    assert [e["type"] for e in events] == ["routine.step", "routine.step"]
    assert events[1]["result"] == "Error: boom" and events[0]["summary"] == "Flip desk"
    assert result["headline"] == "Stopped at step 2/3 (Explode): Error: boom"

    audit = rt.store.list_audit(tool="")
    assert [(a["tool"], a["source"], a["decision"], a["ok"]) for a in reversed(audit)] == [
        ("flip", "routine", "routine", True),
        ("explode", "routine", "routine", False),
    ]
    assert audit[1]["permission"] == "ask"  # Approved with the routine, not per step.
    summary = rt.store.list_messages(result["conversation_id"])[-1]
    assert rt.store.get_conversation(result["conversation_id"])["title"] == "Routines"
    assert "**ROUTINE // LIGHTS** `[FAIL]` 1/3 steps" in summary["content"]
    assert "`[SKIP]` 03 flip" in summary["content"]
    assert routines.get_routine(rt, "Lights")["last_status"] == "failed"

    routine = routines.update_routine(
        rt,
        routine["id"],
        steps=[
            {"tool": "flip", "arguments": {"name": "desk"}},
            {"tool": "explode", "arguments": {}, "continue_on_error": True},
            {"tool": "flip", "arguments": {"name": "hall"}},
        ],
    )
    result = await routines.run_routine(rt, routine)
    assert result["ok"] is True and result["failed_at"] is None
    assert FLIPS[-1] == ("hall", 1)
    assert result["headline"] == "2/3 steps OK, 1 failed (continued)"
    assert result["conversation_id"] == routines.conversation(rt)  # The same Routines chat.


async def test_permission_tiers_still_apply(rt):
    routine = routines.create_routine(rt, "Lights", "", steps(("flip", {"name": "desk"})))
    rt.update_preferences({"tool_permissions": {"flip": "deny"}})
    result = await routines.run_routine(rt, routine)
    assert result["ok"] is False and FLIPS == []
    assert result["steps"][0]["result"] == "Not run: flip is switched off in Settings → Tools."
    assert rt.store.list_audit()[0]["decision"] == "blocked"
    assert routines.describe(rt, routine)["steps"][0]["available"] is False

    rt.update_preferences({"tool_permissions": {"flip": "allow"}})
    del rt.registry.tools["flip"]
    result = await routines.run_routine(rt, routine)
    assert "there is no tool named flip" in result["steps"][0]["result"]


async def test_candidates_and_recording_from_a_chat(rt, mock, recorder):
    agent = Agent(rt)
    mock.script = [
        Reply(tool_calls=[("flip", {"name": "desk"}), ("explode", {})]),
        Reply(text="Desk done, the other one failed."),
    ]
    await agent.run(RunRequest(text="flip the desk switch"), recorder.emit, recorder.approve)
    cid = recorder.of("conversation")[0]["conversation"]["id"]
    found = routines.candidates(rt.store, cid, registry=rt.registry)
    assert [(c["tool"], c["arguments"]) for c in found] == [("flip", {"name": "desk"})]
    assert found[0]["suggested"] is True and found[0]["summary"] == "Flip desk"
    assert found[0]["result"] == "Flipped desk 1x."
    assert rt.store.list_messages(cid)[1]["id"] == found[0]["message_id"]

    with pytest.raises(RoutineError, match="not recording"):
        routines.stop_recording(rt, cid, "Evening")
    assert routines.start_recording(rt, cid)["steps"] == []
    mock.script = [
        Reply(tool_calls=[("flip", {"name": "hall", "times": 2})]),
        Reply(tool_calls=[("flip", {"name": "porch"})]),
        Reply(text="Both flipped."),
    ]
    await agent.run(
        RunRequest(text="now the hall twice and the porch", conversation_id=cid),
        recorder.emit,
        recorder.approve,
    )
    state = routines.recording(rt, cid)
    assert state["recording"] is True and len(state["steps"]) == 2
    saved = routines.stop_recording(rt, cid, "Evening", "Hall and porch")
    assert [s["arguments"] for s in saved["steps"]] == [
        {"name": "hall", "times": 2},
        {"name": "porch"},
    ]
    assert routines.recording(rt, cid)["recording"] is False

    routines.start_recording(rt, cid)
    with pytest.raises(RoutineError, match="Nothing to save"):
        routines.stop_recording(rt, cid, "Empty")
    assert routines.cancel_recording(rt, cid) is True
    with pytest.raises(RoutineError, match="doesn't exist"):
        routines.start_recording(rt, "nope")


async def test_scene_and_routine_tools_ask_once(rt, mock, recorder, desktop):
    agent = Agent(rt)
    mock.script = [
        Reply(tool_calls=[("set_up_scene", {"name": "coding mode"})]),
        Reply(text="Workspace 2 is ready."),
    ]
    await agent.run(RunRequest(text="set up coding mode"), recorder.emit, recorder.approve)
    assert recorder.asked == ["set_up_scene"]  # One approval for the whole scene.
    assert desktop.dispatched() == [
        ["exec", "[workspace 2 silent] kitty"],
        ["exec", "[workspace 2 silent] zed"],
        ["exec", "[workspace 2 silent] kitty --class ctos-term -e lazygit"],
        ["workspace", "2"],
    ]
    end = recorder.of("tool.end")[0]
    assert end["ok"] is True and "**ROUTINE // CODING** `[OK]` 4/4 steps" in end["result"]
    assert routines._meta(rt.store, "conversation") is None  # The chat got the result itself.

    recorder.asked.clear()
    routines.create_routine(rt, "Lights", "", steps(("flip", {"name": "desk"})))
    mock.script = [
        Reply(tool_calls=[("run_routine", {"name": "lights"})]),
        Reply(tool_calls=[("run_routine", {"name": "disco"})]),
        Reply(text="Done."),
    ]
    await agent.run(RunRequest(text="lights routine again"), recorder.emit, recorder.approve)
    assert recorder.asked == ["run_routine", "run_routine"] and FLIPS == [("desk", 1)]
    ends = recorder.of("tool.end")[1:]
    assert ends[0]["ok"] is True and "There is no routine called 'disco'" in ends[1]["result"]


async def test_save_routine_tool(rt, mock, recorder):
    save = rt.registry.get("save_routine")
    assert save.risk == "confirm" and save.parameters["required"] == ["name", "steps"]
    mock.script = [
        Reply(
            tool_calls=[
                (
                    "save_routine",
                    {"name": "Porch", "steps": [{"tool": "flip", "arguments": {"name": "porch"}}]},
                )
            ]
        ),
        Reply(tool_calls=[("save_routine", {"name": "Bad", "steps": [{"tool": "shred"}]})]),
        Reply(
            tool_calls=[
                (
                    "save_routine",
                    {
                        "name": "porch",
                        "steps": '[{"tool": "flip", "arguments": {"name": "porch", "times": 3}}]',
                        "replace": True,
                    },
                )
            ]
        ),
        Reply(text="Saved."),
    ]
    await Agent(rt).run(RunRequest(text="save that as a routine"), recorder.emit, recorder.approve)
    results = [e["result"] for e in recorder.of("tool.end")]
    assert results[0].startswith("Saved routine “Porch”")
    assert "no tool named 'shred'" in results[1]
    assert routines.get_routine(rt, "Porch")["steps"][0]["arguments"]["times"] == 3
    assert routines.get_routine(rt, "Bad") is None


async def test_routine_automation_kind(rt):
    seen = []

    async def listener(event):
        seen.append(event)

    rt.listeners.add(listener)
    lights = routines.create_routine(rt, "Lights", "", steps(("flip", {"name": "desk"})))
    with pytest.raises(ScheduleError, match="no routine #999"):
        rt.scheduler.create("routine", "Ghost", "every 1 hour", target="999")
    with pytest.raises(ScheduleError, match="Choose the routine"):
        rt.scheduler.create("routine", "Ghost", "every 1 hour", target="lights")
    item = rt.scheduler.create("routine", "Lights", "every 1 hour", target=str(lights["id"]))
    rt.store.update_automation(item["id"], next_run=time.time() - 1)
    await rt.scheduler.tick()
    done = rt.store.get_automation(item["id"])
    assert done["last_status"] == "ok" and done["last_result"] == "1/1 steps OK"
    assert done["conversation_id"] == routines.conversation(rt) and FLIPS == [("desk", 1)]
    note = next(e for e in seen if e["type"] == "notification")
    assert note["title"] == "ROUTINE // LIGHTS" and note["level"] == "info"
    assert "scheduled" in rt.store.list_audit()[0]["detail"]

    routines.update_routine(rt, lights["id"], steps=steps(("explode", {})))
    rt.store.update_automation(item["id"], next_run=time.time() - 1)
    await rt.scheduler.tick()
    done = rt.store.get_automation(item["id"])
    assert done["last_status"] == "error" and "boom" in done["last_result"]
    notes = [e for e in seen if e["type"] == "notification"]
    assert notes[-1]["level"] == "important"

    routines.delete_routine(rt, lights["id"])
    paused = rt.store.get_automation(item["id"])
    assert paused["enabled"] is False and paused["last_result"] == "Its routine was deleted."


def test_api(rt):
    with TestClient(create_app(rt), base_url="http://localhost") as client:
        listed = client.get("/api/routines").json()
        assert [r["name"] for r in listed] == ["coding"]
        assert listed[0]["steps"][2]["summary"] == "Open lazygit"

        created = client.post(
            "/api/routines",
            json={
                "name": "Lights",
                "description": "Desk then hall",
                "steps": [
                    {"tool": "flip", "arguments": {"name": "desk"}, "note": "first"},
                    {"tool": "flip", "arguments": {"name": "hall", "times": "2"}},
                ],
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["steps"][1]["arguments"] == {"name": "hall", "times": 2}
        assert body["steps"][0]["risk"] == "confirm" and body["steps"][0]["available"] is True
        bad = client.post(
            "/api/routines", json={"name": "X", "steps": [{"tool": "shred", "arguments": {}}]}
        )
        assert bad.status_code == 422 and "no tool named" in bad.json()["detail"]

        rid = body["id"]
        assert client.get("/api/routines/lights").json()["id"] == rid
        assert client.get("/api/routines/404").status_code == 404
        renamed = client.patch(f"/api/routines/{rid}", json={"name": "Hall lights"}).json()
        assert renamed["name"] == "Hall lights" and len(renamed["steps"]) == 2
        clash = client.patch(f"/api/routines/{rid}", json={"name": "coding"})
        assert clash.status_code == 422

        lines = client.post(f"/api/routines/{rid}/run", json={"source": "web"}).text
        events = [json.loads(line) for line in lines.splitlines()]
        assert [e["type"] for e in events] == [
            "routine.start",
            "routine.step",
            "routine.step",
            "routine.end",
        ]
        assert events[0]["total"] == 2 and events[2]["result"] == "Flipped hall 2x."
        end = events[-1]
        assert end["ok"] is True and end["failed_at"] is None and len(end["steps"]) == 2

        chat = rt.store.create_conversation("Chores")
        assert (
            client.get("/api/routines/candidates", params={"conversation_id": "x"}).status_code
            == 404
        )
        assert (
            client.get("/api/routines/candidates", params={"conversation_id": chat["id"]}).json()
            == []
        )
        start = client.post(
            "/api/routines/record", json={"conversation_id": chat["id"], "action": "start"}
        ).json()
        assert start["recording"] is True and start["steps"] == []
        rt.store.add_message(
            chat["id"],
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "flip", "arguments": '{"name": "attic"}'},
                }
            ],
        )
        rt.store.add_message(
            chat["id"],
            "tool",
            "Flipped attic 1x.",
            tool_call_id="c1",
            name="flip",
            meta={"ok": True},
        )
        state = client.get("/api/routines/record", params={"conversation_id": chat["id"]}).json()
        assert [s["tool"] for s in state["steps"]] == ["flip"]
        assert (
            client.post(
                "/api/routines/record", json={"conversation_id": chat["id"], "action": "stop"}
            ).status_code
            == 422
        )
        stopped = client.post(
            "/api/routines/record",
            json={"conversation_id": chat["id"], "action": "stop", "name": "Attic"},
        ).json()
        assert stopped["recording"] is False and stopped["routine"]["name"] == "Attic"

        made = client.post(
            "/api/automations",
            json={
                "kind": "routine",
                "name": "Lights",
                "schedule": "daily at 07:00",
                "target": str(rid),
            },
        )
        assert made.status_code == 201
        missing = client.post(
            "/api/automations",
            json={"kind": "routine", "name": "X", "schedule": "daily at 07:00", "target": "999"},
        )
        assert missing.status_code == 422

        assert client.delete(f"/api/routines/{rid}").status_code == 204
        assert client.get(f"/api/routines/{rid}").status_code == 404
        assert client.post(f"/api/routines/{rid}/run").status_code == 404


class NoTimeouts:
    """A TestClient that ignores per-request timeouts, which it does not support."""

    def __init__(self, http) -> None:
        self._http = http

    def __getattr__(self, name):
        method = getattr(self._http, name)

        def call(*args, timeout=None, **kwargs):
            return method(*args, **kwargs)

        return call


class LocalClient(Client):
    """A ``Client`` that talks to a TestClient instead of the network."""

    def __init__(self, http) -> None:
        self.url = "http://localhost"
        self.http = NoTimeouts(http)

    def close(self) -> None:
        pass


def test_cli_through_the_server(rt, monkeypatch, capsys):
    routines.create_routine(rt, "Lights", "Desk lamp", steps(("flip", {"name": "desk"})))
    with TestClient(create_app(rt), base_url="http://localhost") as http:
        monkeypatch.setattr(routine_cli, "Client", lambda: LocalClient(http))

        def run(*argv: str) -> int:
            return main(["routine", *argv])

        assert run("list") == 0
        out = capsys.readouterr().out
        assert "» ROUTINES [02]" in out and "Lights" in out and "01 STEPS" in out
        assert run("show", "lights") == 0
        assert '01  Flip desk  flip {"name": "desk"}' in capsys.readouterr().out

        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert run("run", "lights") == 1 and FLIPS == []
        assert "ABORTED" in capsys.readouterr().out
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        assert run("run", "lights") == 0 and FLIPS == [("desk", 1)]
        out = capsys.readouterr().out
        assert "[OK]   01  Flip desk: Flipped desk 1x." in out
        assert "[OK] ROUTINE COMPLETE 01/01" in out
        assert run("run", "ghost", "--yes") == 1
        assert "NO ROUTINE NAMED 'ghost'" in capsys.readouterr().err
        assert run("delete", "Lights") == 0
        assert run("show", "lights") == 1
        assert routine_cli.Paint().on is False  # Captured output is not a terminal.


def test_cli_without_a_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BAGLEY_URL", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "data"))
    plugins = tmp_path / "data" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "stamp.py").write_text(
        "from bagley.tools import ToolError, tool\n\n"
        "@tool(risk='confirm', summary='Stamp {label}')\n"
        "def stamp(label: str) -> str:\n"
        "    if label == 'fail':\n"
        "        raise ToolError('ink is out')\n"
        "    return f'Stamped {label}.'\n"
    )
    rt = Runtime(ServerConfig.from_env())
    try:
        routines.create_routine(
            rt, "Paperwork", "", steps(("stamp", {"label": "a"}), ("stamp", {"label": "fail"}))
        )
    finally:
        rt.store.close()

    assert main(["routine", "show", "paperwork"]) == 0
    assert "02  Stamp fail" in capsys.readouterr().out
    assert main(["routine", "run", "paperwork", "--yes"]) == 1
    out = capsys.readouterr().out
    assert "[OK]   01  Stamp a: Stamped a." in out
    assert "[FAIL] 02  Stamp fail: Error: ink is out" in out
    assert "[CRIT] HALTED AT STEP 02/02" in out
    assert main(["routine", "delete", "paperwork"]) == 0
    assert "[OK] DELETED Paperwork" in capsys.readouterr().out
    assert main(["routine"]) == 0
    assert "Paperwork" not in capsys.readouterr().out
