from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from bagley import bench, cli
from bagley.bench import (
    BenchResult,
    MachineResult,
    ModelResult,
    TaskResult,
    apply_winners,
    latest_results,
    run_bench,
    select_tasks,
)
from bagley.commands.audit import Paint
from bagley.commands.bench import Progress
from bagley.llm import ToolCall
from bagley.server import create_app
from tests.demo_stack import free_port, serve_in_thread
from tests.mock_llm import MockLLM, Reply

# What a capable model does for each task: tool calls per step, then the answer.
PLANS: dict[str, tuple[list[list[tuple[str, dict]]], str]] = {
    "weather_lisbon": ([[("get_weather", {"location": "Lisbon"})]], "22°C and clear in Lisbon."),
    "tip_math": ([[("calculate", {"expression": "2340 * 17.5 / 100"})]], "The tip is €409.50."),
    "time_tokyo": ([[("get_current_time", {"timezone": "Asia/Tokyo"})]], "It's 16:43 in Tokyo."),
    "find_notes": (
        [[("search_files", {"query": "Lisbon trip"})]],
        "In notes/trips/lisbon-2026.md.",
    ),
    "marathon_km": (
        [[("convert_units", {"value": "26.2", "from_unit": "miles", "to_unit": "km"})]],
        "About 42.16 km.",
    ),
    "lisbon_fahrenheit": (
        [
            [("get_weather", {"location": "Lisboa, Portugal"})],
            [("convert_units", {"value": 22, "from_unit": "°C", "to_unit": "F"})],
        ],
        "It's 71.6°F in Lisbon.",
    ),
    "capital_no_tool": ([], "Paris."),
    "calendar_pick": (
        [
            [
                (
                    "create_calendar_event",
                    {"title": "Dentist", "date": "2026-10-14", "time": "10:00"},
                )
            ]
        ],
        "Booked.",
    ),
    "messy_email": (
        [
            [
                (
                    "send_email",
                    {
                        "to": "maria.santos@example.com",
                        "subject": "Lunch Friday?",
                        "body": "Running 10 minutes late, sorry!",
                    },
                )
            ]
        ],
        "Sent.",
    ),
    "compare_cities": (
        [[("get_weather", {"location": "Lisbon"}), ("get_weather", {"location": "Oslo"})]],
        "Lisbon is warmer: 22°C against 9°C in Oslo.",
    ),
}


def task_of(messages: list[dict]) -> str:
    prompt = next(m["content"] for m in messages if m["role"] == "user")
    return next(t.id for t in bench.TASKS if t.prompt == prompt)


def brain(weak: set[str] = frozenset({"llama3.2:3b"})):
    """A scripted model server: strong models follow PLANS, weak ones guess without tools."""

    def reply(payload: dict) -> Reply:
        messages = payload["messages"]
        if messages[-1]["content"] == "Reply with OK.":
            return Reply(text="OK")
        if payload["model"] in weak:
            return Reply(text="Paris, I think.")
        calls, answer = PLANS[task_of(messages)]
        step = sum(m["role"] == "assistant" for m in messages)
        return Reply(tool_calls=calls[step]) if step < len(calls) else Reply(text=answer)

    return reply


class Unplugged(httpx.AsyncBaseTransport):
    """The mock transport, except that some hosts refuse connections."""

    def __init__(self, inner: httpx.AsyncBaseTransport, down: set[str]) -> None:
        self.inner, self.down = inner, down

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self.down:
            raise httpx.ConnectError("Connection refused", request=request)
        return await self.inner.handle_async_request(request)


DESK = {
    "id": "desk",
    "name": "Desk",
    "role": "gpu",
    "provider": "ollama",
    "base_url": "http://mock2",
}


# Checks and scoring ------------------------------------------------------------------------------


def test_tasks_and_stub_tools():
    assert 8 <= len(bench.TASKS) <= 12 and len({t.id for t in bench.TASKS}) == len(bench.TASKS)
    assert set(PLANS) == {t.id for t in bench.TASKS}
    assert bench.run_stub(bench.calculate, {"expression": "2340 * 0.175"}) == "2340 * 0.175 = 409.5"
    assert "Error: Unknown unit" in bench.run_stub(
        bench.convert_units, {"value": 1, "from_unit": "parsec", "to_unit": "km"}
    )
    assert '"value": 71.6' in bench.run_stub(
        bench.convert_units, {"value": "22", "from_unit": "celsius", "to_unit": "°F"}
    )
    assert "08:43" in bench.run_stub(bench.get_current_time, {})  # Lisbon by default.
    tokyo = bench.run_stub(bench.get_current_time, {"timezone": "Asia/Tokyo"})
    assert "Wednesday, 07 October 2026, 16:43" in tokyo and "+09:00" in tokyo
    assert "Missing required" in bench.run_stub(bench.get_weather, {"city": "Lisbon"})
    with pytest.raises(ValueError, match="Unknown task"):
        select_tasks(["weather_lisbon", "nope"])


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


def test_judging_is_tolerant_but_strict_about_the_point():
    (weather,) = select_tasks(["weather_lisbon"])
    assert weather.judge([call("get_weather", location="Lisboa, PT")], "It is 22 °C.") == ""
    assert weather.judge([call("get_weather", location="Porto")], "22") == (
        'get_weather got location="Porto", want lisbon|lisboa'
    )
    assert weather.judge([], "Probably 22") == "no call to get_weather"
    assert weather.judge([call("web_search", query="lisbon weather")], "22").startswith(
        "called web_search"
    )
    assert (
        weather.judge([call("get_weather", location="Lisbon")], "Sunny!")
        == "answer lacks /\\b22\\b/"
    )

    (tip,) = select_tasks(["tip_math"])
    for expression in ("0.175 * 2,340", "2340*17.5/100", "409.5"):
        assert tip.judge([call("calculate", expression=expression)], "409.50") == ""
    (marathon,) = select_tasks(["marathon_km"])  # Either tool will do.
    assert marathon.judge([call("calculate", expression="26.2 * 1.609344")], "42.16 km") == ""
    (capital,) = select_tasks(["capital_no_tool"])
    assert capital.judge([call("web_search", query="capital of France")], "Paris").startswith(
        "used web_search"
    )
    assert capital.judge([], "") == "no final answer"


def model(name: str, passes: int, seconds: float, total: int = 10) -> ModelResult:
    tasks = [TaskResult(task=f"t{i}", ok=i < passes, seconds=seconds) for i in range(total)]
    return ModelResult(model=name, tasks=tasks)


def test_scoring_and_winner_selection():
    assert model("a", 10, 2.0).score == 98.0
    assert model("slow", 10, 60.0).score == 90.0  # The latency penalty is capped.
    assert model("b", 9, 0.5).score == 89.5
    machine = MachineResult(
        id="local", name="B1T", role="local", models=[model("b", 9, 0.5), model("a", 10, 2.0)]
    )
    assert machine.pick_winner() == "a" and machine.models[1].winner
    tie = MachineResult(
        id="desk",
        name="DESK",
        role="gpu",
        models=[model("x", 8, 1.04), model("y", 8, 0.96), model("none", 0, 0.1)],
    )
    assert [m.score for m in tie.models] == [79.0, 79.0, -0.1]
    assert tie.pick_winner() == "y"  # Same score: the faster one.
    nothing = MachineResult(id="z", name="Z", role="gpu", models=[model("none", 0, 0.1)])
    assert nothing.pick_winner() == ""


# Runs against the mock model server --------------------------------------------------------------


@pytest.mark.anyio
async def test_bench_two_machines_picks_and_applies_winners(make_runtime, mock):
    mock.next_reply = brain()
    rt = make_runtime(model="", machines=[DESK])
    events: list[dict] = []
    result = await run_bench(rt, models=["qwen3:8b", "llama3.2"], progress=events.append)

    assert [m.id for m in result.machines] == ["local", "desk"]
    for machine in result.machines:
        weak, strong = machine.models  # In the server's (alphabetical) order.
        assert (strong.model, strong.mode, strong.passed) == ("qwen3:8b", "native", 10)
        assert (weak.model, weak.passed) == ("llama3.2:3b", 1)
        assert machine.winner == "qwen3:8b" and strong.winner and not weak.winner
        failed = {t.task: t.reason for t in weak.tasks if not t.ok}
        assert failed["weather_lisbon"] == "no call to get_weather"
        assert strong.tokens_per_second == 42.0 and strong.ttft is not None
    strong = result.machines[0].models[1]
    first = next(t for t in strong.tasks if t.task == "compare_cities")
    assert first.steps == 2 and [c["name"] for c in first.calls] == ["get_weather"] * 2

    kinds = [e["type"] for e in events]
    assert kinds[0] == "bench.start" and events[0]["total"] == 40
    assert kinds.count("bench.task") == 40 and kinds.count("bench.model") == 4
    task_events = [e for e in events if e["type"] == "bench.task"]
    assert [e["index"] for e in task_events] == list(range(1, 41))
    assert (
        task_events[0]["machine"] == result.machines[0].name
        and task_events[-1]["machine"] == "DESK"
    )
    last = [e for e in events if e["type"] == "bench.model"][-1]
    assert (last["model"], last["index"], last["total"]) == ("qwen3:8b", 40, 40)
    assert (
        last["result"]["passed"] == last["result"]["total"] == 10
        and last["result"]["winner"] is False
    )

    saved = latest_results(rt.store)
    assert [(m["machine"], m["winner"]) for m in saved] == [
        ("desk", "qwen3:8b"),
        ("local", "qwen3:8b"),
    ]
    assert saved[0]["models"][0]["passed"] == 10 and len(saved[0]["models"][0]["tasks"]) == 10

    changes = apply_winners(rt, result)
    assert changes == {"applied": {"local": "qwen3:8b", "desk": "qwen3:8b"}, "skipped": {}}
    prefs, _ = rt.preferences()
    assert prefs.model == "qwen3:8b" and prefs.machines[0].model == "qwen3:8b"


@pytest.mark.anyio
async def test_prompt_mode_for_models_without_tools(make_runtime, mock):
    mock.next_reply = brain()
    rt = make_runtime()
    result = await run_bench(rt, models=["gemma2*"], tasks=["lisbon_fahrenheit", "capital_no_tool"])
    [gemma] = result.machines[0].models
    assert (gemma.model, gemma.mode, gemma.passed) == ("gemma2:2b", "prompt", 2)
    sent = [r for r in mock.requests if r["model"] == "gemma2:2b" and len(r["messages"]) > 1]
    assert all("tools" not in r for r in sent)
    assert "<tool_call>" in sent[0]["messages"][0]["content"]
    assert '<tool_response name="get_weather">' in sent[1]["messages"][-1]["content"]


@pytest.mark.anyio
async def test_falls_back_to_prompt_mode_when_the_server_refuses_tools(make_runtime, mock):
    # An older server that doesn't report capabilities: try native tools, then the text protocol.
    mock.models["mystery:1b"] = {**mock.models["gemma2:2b"], "capabilities": "completion"}
    mock.next_reply = brain()
    rt = make_runtime()
    result = await run_bench(rt, models=["mystery*"], tasks=["weather_lisbon", "capital_no_tool"])
    [mystery] = result.machines[0].models
    assert (mystery.mode, mystery.passed) == ("prompt", 2)


@pytest.mark.anyio
async def test_unreachable_machines_and_unknown_models_are_skipped(make_runtime, mock):
    mock.next_reply = brain()
    rt = make_runtime(machines=[DESK, {**DESK, "id": "attic", "base_url": "http://offline"}])
    rt.llm_transport = Unplugged(rt.llm_transport, {"offline"})
    events: list[dict] = []
    result = await run_bench(
        rt,
        machines=["attic", "desk"],
        models=["qwen3:8b"],
        tasks=["capital_no_tool"],
        progress=events.append,
    )
    desk, attic = result.machines
    assert desk.winner == "qwen3:8b"
    assert attic.models == [] and "Can't reach" in attic.note
    assert events[0]["machines"][1]["note"] == attic.note
    with pytest.raises(ValueError, match="Unknown or disabled machine"):
        await run_bench(rt, machines=["garage"])


@pytest.mark.anyio
async def test_a_model_that_fails_to_load(make_runtime, mock, capsys):
    def reply(payload):
        if payload["model"] == "llama3.2:3b":
            return Reply(error=(500, "model requires more system memory (20 GiB)"))
        return brain()(payload)

    mock.next_reply = reply
    rt = make_runtime()
    events: list[dict] = []
    result = await run_bench(
        rt, models=["qwen3:8b", "llama3.2:3b"], tasks=["capital_no_tool"], progress=events.append
    )
    broken = next(m for m in result.machines[0].models if m.model == "llama3.2:3b")
    assert "more system memory" in broken.error and broken.tasks == [] and broken.score == 0
    assert result.machines[0].winner == "qwen3:8b"
    [failed] = [e for e in events if e["type"] == "bench.model" and e["result"]["error"]]
    assert (failed["model"], failed["index"], failed["total"]) == ("llama3.2:3b", 1, 2)
    Progress(Paint(on=False))(failed)
    assert "llama3.2:3b LOAD FAIL  Ollama error: model requires" in capsys.readouterr().out


def test_apply_respects_the_environment(make_runtime):
    rt = make_runtime(env={"BAGLEY_MODEL": "qwen3:8b"})
    result = BenchResult(run_id="r", started_at=0, tasks=[])
    result.machines.append(MachineResult(id="local", name="L", role="local", winner="gemma2:2b"))
    assert apply_winners(rt, result) == {
        "applied": {},
        "skipped": {"local": "BAGLEY_MODEL sets this machine's model"},
    }


# HTTP API ----------------------------------------------------------------------------------------


def test_bench_api_streams_progress_and_keeps_results(make_runtime, mock):
    mock.next_reply = brain()
    rt = make_runtime(model="", machines=[DESK])
    with TestClient(create_app(rt), base_url="http://localhost") as client:
        assert client.get("/api/bench").json()["machines"] == []
        resp = client.post(
            "/api/bench/run",
            json={
                "machines": ["desk"],
                "models": ["qwen3:8b"],
                "tasks": ["weather_lisbon"],
                "apply": True,
            },
        )
        assert resp.status_code == 200 and resp.headers["content-type"] == "application/x-ndjson"
        events = [json.loads(line) for line in resp.text.splitlines()]
        assert [e["type"] for e in events] == [
            "bench.start",
            "bench.task",
            "bench.model",
            "bench.done",
        ]
        assert events[1]["ok"] and events[1]["task"] == "weather_lisbon" and events[1]["index"] == 1
        done = events[-1]
        assert done["applied"] == {"applied": {"desk": "qwen3:8b"}, "skipped": {}}
        assert done["result"]["machines"][0]["winner"] == "qwen3:8b"

        body = client.get("/api/bench").json()
        assert body["running"] is False and len(body["tasks"]) == len(bench.TASKS)
        [desk] = body["machines"]
        assert desk["machine"] == "desk" and desk["name"] == "DESK" and desk["winner"] == "qwen3:8b"
        assert desk["models"][0]["tasks"][0]["calls"] == [
            {"name": "get_weather", "arguments": {"location": "Lisbon"}}
        ]
        assert rt.preferences()[0].machines[0].model == "qwen3:8b"

        assert client.post("/api/bench/run", json={"tasks": ["nope"]}).status_code == 422
        error = client.post("/api/bench/run", json={"machines": ["garage"]}).text
        assert json.loads(error.splitlines()[-1])["type"] == "error"

        from bagley.api.bench import _runs

        runs = _runs(rt)
        asyncio.run(runs.lock.acquire())
        assert client.post("/api/bench/run", json={}).status_code == 409
        runs.lock.release()


# CLI ---------------------------------------------------------------------------------------------


@pytest.fixture
def bench_cli(tmp_path, monkeypatch):
    mock = MockLLM()
    mock.next_reply = brain()
    port = free_port()
    server = serve_in_thread(mock.app, port)
    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BAGLEY_URL", f"http://127.0.0.1:{free_port()}")  # No Bagley server.
    from bagley.store import Store

    store = Store(tmp_path / "data" / "bagley.db")
    store.set_preferences(
        {"provider": "ollama", "base_url": f"http://127.0.0.1:{port}", "model": "llama3.2:3b"}
    )
    store.close()
    yield tmp_path / "data" / "bagley.db"
    server.should_exit = True


def test_bench_cli_runs_in_process_and_applies(bench_cli, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    argv = ["bench", "--models", "qwen3:8b", "llama3.2:3b", "--tasks", "weather_lisbon", "tip_math"]
    assert cli.main([*argv, "--apply"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("BENCH ") and lines[0].endswith("2 TASKS // 4 RUNS")
    assert lines[1].startswith("[01/04] ") and "llama3.2:3b weather_lisbon ...." in lines[1]
    assert lines[1].endswith("no call to get_weather") and " FAIL " in lines[1]
    assert lines[3].startswith("[03/04] ") and "qwen3:8b weather_lisbon" in lines[3]
    assert " OK " in lines[3] and lines[3].endswith("s")
    assert ">  qwen3:8b" in out and "02/02" in out and "00/02" in out
    assert "[OK] APPLIED  LOCAL -> qwen3:8b" in out

    from bagley.store import Store

    store = Store(bench_cli)
    assert store.get_preferences()["model"] == "qwen3:8b"
    store.close()

    assert cli.main([*argv, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] is None and data["result"]["machines"][0]["winner"] == "qwen3:8b"

    assert cli.main(["bench", "--tasks", "nope"]) == 2
    assert "Unknown task" in capsys.readouterr().err
    assert cli.main(["bench", "--list-tasks"]) == 0
    assert "weather_lisbon" in capsys.readouterr().out
