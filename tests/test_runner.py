from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bagley import __version__, cli, runner
from bagley.config import ServerConfig
from bagley.runtime import Runtime
from bagley.server import create_app
from tests.demo_stack import free_port
from tests.mock_llm import fake_web_transport

RUNNER_HOST = "surface.tail1234.ts.net"
RUNNER_URL = f"https://{RUNNER_HOST}"
TOKEN = "runner-secret-token"
ROUTINES = """CREATE TABLE IF NOT EXISTS routines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    steps      TEXT NOT NULL DEFAULT '[]',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
)"""


class Network(httpx.AsyncBaseTransport):
    """Routes the laptop's outgoing requests: the runner host to a second in-process Bagley,
    everything else to the fake web. Records every request."""

    def __init__(self, runner_app) -> None:
        self.runner = httpx.ASGITransport(app=runner_app)
        self.web = fake_web_transport()
        self.seen: list[httpx.Request] = []
        self.down = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.host != RUNNER_HOST:
            return await self.web.handle_async_request(request)
        if self.down:
            raise httpx.ConnectError("Connection refused", request=request)
        return await self.runner.handle_async_request(request)


@pytest.fixture
def surface(tmp_path, mock):
    """The always-on runner: its own data, a token, and the tailnet name allowed."""
    config = ServerConfig(
        data_dir=tmp_path / "surface",
        workspace=tmp_path / "surface-ws",
        token=TOKEN,
        allowed_hosts=[RUNNER_HOST],
    )
    rt = Runtime(
        config,
        env={},
        llm_transport=httpx.ASGITransport(app=mock.app),
        tool_transport=fake_web_transport(),
    )
    rt.store.set_preferences({"provider": "ollama", "base_url": "http://mock", "model": "qwen3:8b"})
    app = create_app(rt)
    app.state.runtime = rt  # ASGITransport doesn't run the lifespan that sets it.
    yield rt, app
    rt.store.close()


@pytest.fixture
def laptop(make_runtime, surface):
    """The laptop's API, with its HTTP client wired to the runner over the fake network."""
    rt = make_runtime(runner_url=RUNNER_URL, runner_token=TOKEN)
    network = Network(surface[1])
    rt.http = httpx.AsyncClient(transport=network, timeout=20.0)
    with TestClient(create_app(rt), base_url="http://localhost") as client:
        client.runtime = rt
        client.network = network
        client.surface = surface[0]
        yield client


def seed(rt: Runtime) -> dict[str, dict]:
    sched = rt.scheduler
    items = {
        "task": sched.create(
            "task", "Lisbon weather", "every 2 hours", prompt="Weather in Lisbon?"
        ),
        "watch": sched.create(
            "watch", "Laptop price", "every 30 minutes", target="http://93.184.215.14/watched"
        ),
        "reminder": sched.create("reminder", "Bins", "mondays at 07:00", prompt="Take out bins"),
        "off": sched.create("task", "Paused digest", "daily at 08:00", prompt="Digest"),
    }
    rt.store.update_automation(items["off"]["id"], enabled=False)
    rt.store.update_automation(items["task"]["id"], state={"hash": "abc"}, last_status="ok")
    rt.store.create_automation(  # A reminder that already fired: nothing left to move.
        kind="reminder", name="Old", prompt="Old", schedule="at 2001-01-01 10:00", enabled=False
    )
    rt.store.add_memory("Prefers metric units")
    rt.store.add_memory("Lives in Lisbon")
    return items


# Export and import ------------------------------------------------------------------------------


def test_export_import_round_trip_and_dedupe(make_runtime, surface):
    laptop = make_runtime()
    seed(laptop)
    laptop.store.ensure_schema(ROUTINES)
    laptop.store.execute(
        "INSERT INTO routines (name, steps, created_at) VALUES (?, ?, ?)",
        ("Morning", '["weather", "news"]', 1.0),
    )
    data = json.loads(json.dumps(runner.export_data(laptop)))  # Survives JSON.

    assert data["version"] == runner.EXPORT_VERSION and data["exported_at"]
    assert [a["name"] for a in data["automations"]] == [
        "Lisbon weather",
        "Laptop price",
        "Bins",
        "Paused digest",
    ]
    for item in data["automations"]:
        assert set(item) == {"kind", "name", "prompt", "schedule", "target", "enabled"}
    assert data["memories"] == [{"content": "Prefers metric units"}, {"content": "Lives in Lisbon"}]
    assert data["routines"] == [{"name": "Morning", "steps": '["weather", "news"]', "enabled": 1}]

    target = surface[0]
    target.store.ensure_schema(ROUTINES)
    target.store.add_memory("lives in   LISBON")  # Same memory, different spacing and case.
    result = runner.import_data(target, data)
    assert result["added"] == {"automations": 4, "memories": 1, "routines": 1}
    assert result["skipped"] == {"automations": 0, "memories": 1, "routines": 0}
    assert result["errors"] == []
    imported = {a["name"]: a for a in target.store.list_automations()}
    assert imported["Laptop price"]["target"] == "http://93.184.215.14/watched"
    assert imported["Lisbon weather"]["schedule"] == "every 120 minutes"
    assert imported["Lisbon weather"]["state"] == {} and imported["Lisbon weather"]["enabled"]
    assert imported["Paused digest"]["enabled"] is False
    assert imported["Bins"]["conversation_id"] is None
    routine = target.store.query_one("SELECT * FROM routines")
    assert routine["name"] == "Morning" and routine["created_at"] > 1.0

    again = runner.import_data(target, data)
    assert again["added"] == {"automations": 0, "memories": 0, "routines": 0}
    assert again["skipped"] == {"automations": 4, "memories": 2, "routines": 1}


def test_import_without_a_routines_table(make_runtime):
    rt = make_runtime()
    result = runner.import_data(rt, {"version": 1, "routines": [{"name": "Morning"}]})
    assert result["skipped"]["routines"] == 1
    assert result["errors"] == [
        {"part": "routines", "name": "", "error": "Routines aren't set up here."}
    ]
    assert "routines" not in runner.export_data(rt)  # No table: nothing to export.


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "Expected a JSON object"),
        ({"version": 99}, "Unsupported export version"),
        ({"mode": "replace"}, "Only mode 'merge'"),
        ({"automations": {"kind": "task"}}, "must be a list"),
        ({"automations": [{"kind": "Task!", "schedule": "hourly"}]}, "automations[0].kind"),
        ({"automations": [{"kind": "task", "schedule": "x" * 200}]}, "automations[0].schedule"),
        ({"memories": [{"content": ""}]}, "memories[0].content"),
        ({"memories": [""] * 6000}, "Too many memories"),
        ({"routines": [{"steps": "[]"}]}, "routines[0] needs a name"),
        ({"routines": [{"name": "x", "steps": ["a"]}]}, "isn't short text"),
    ],
)
def test_import_rejects_bad_shapes(make_runtime, data, message):
    rt = make_runtime()
    with pytest.raises(runner.DataError, match=message.replace("[", r"\[").replace("]", r"\]")):
        runner.import_data(rt, data)
    assert rt.store.list_automations() == [] and rt.store.list_memories() == []


def test_import_skips_what_this_machine_cannot_run(make_runtime):
    rt = make_runtime()
    result = runner.import_data(
        rt,
        {
            "automations": [
                {"kind": "teleport", "name": "Beam", "schedule": "hourly"},
                {"kind": "task", "name": "Gone", "prompt": "x", "schedule": "at 2001-01-01 10:00"},
                {"kind": "watch", "name": "File", "schedule": "hourly", "target": "file:///etc"},
                {"kind": "task", "name": "Fine", "prompt": "Do it", "schedule": "hourly"},
            ]
        },
    )
    assert result["added"]["automations"] == 1
    assert [(e["name"], e["error"][:20]) for e in result["errors"]] == [
        ("Beam", "Unknown kind 'telepo"),
        ("Gone", "That time is in the "),
        ("File", "A watcher needs an h"),
    ]
    assert [a["name"] for a in rt.store.list_automations()] == ["Fine"]


def test_export_and_import_endpoints(laptop):
    seed(laptop.runtime)
    body = laptop.get("/api/export", params={"parts": "memories"}).json()
    assert "automations" not in body and len(body["memories"]) == 2
    assert laptop.get("/api/export", params={"parts": "secrets"}).status_code == 422

    assert laptop.post("/api/import", content=b"{not json").status_code == 400
    bad = laptop.post("/api/import", json={"automations": [{"kind": "task"}]})
    assert bad.status_code == 422 and "schedule" in bad.json()["detail"]
    ok = laptop.post("/api/import", json={"memories": ["Has a cat", "prefers METRIC units"]})
    assert ok.json()["added"]["memories"] == 1 and ok.json()["skipped"]["memories"] == 1


# The runner -------------------------------------------------------------------------------------


def test_runner_status(laptop):
    laptop.surface.scheduler.create("task", "Already there", "hourly", prompt="x")
    info = laptop.get("/api/runner").json()
    assert info["configured"] and info["reachable"] and info["error"] is None
    assert info["url"] == RUNNER_URL and info["version"] == __version__
    assert info["automations"] == 1 and isinstance(info["latency_ms"], int)

    laptop.network.down = True
    info = laptop.get("/api/runner").json()
    assert info["reachable"] is False and "Can't reach the runner" in info["error"]

    laptop.runtime.update_preferences({"runner_url": ""})
    info = laptop.get("/api/runner").json()
    assert info["configured"] is False and "No runner configured" in info["error"]


def test_move_to_runner(laptop):
    rt = laptop.runtime
    items = seed(rt)
    aid = items["watch"]["id"]
    moved = laptop.post(f"/api/automations/{aid}/move-to-runner").json()
    assert moved["ok"] and moved["runner"] == RUNNER_URL

    there = laptop.surface.store.get_automation(moved["runner_id"])
    for key in ("kind", "name", "prompt", "schedule", "target"):
        assert there[key] == items["watch"][key]
    assert there["enabled"]
    here = rt.store.get_automation(aid)
    assert here["enabled"] is False
    assert here["state"]["moved_to"] == RUNNER_URL and here["state"]["runner_id"] == there["id"]

    again = laptop.post(f"/api/automations/{aid}/move-to-runner")
    assert again.status_code == 409 and "Already moved" in again.json()["detail"]
    assert laptop.post("/api/automations/999/move-to-runner").status_code == 404

    # Running the watcher here fetches the page through the same client, without the token.
    assert laptop.post(f"/api/automations/{aid}/run").status_code == 202
    deadline = time.monotonic() + 10
    while rt.store.get_automation(aid)["last_run"] is None and time.monotonic() < deadline:
        time.sleep(0.05)
    for request in laptop.network.seen:
        to_runner = request.url.host == RUNNER_HOST
        assert ("authorization" in request.headers) == to_runner
        if to_runner:
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert any(r.url.host == "93.184.215.14" for r in laptop.network.seen)


@pytest.mark.parametrize("failure", ["down", "token", "not_bagley"])
def test_failed_move_keeps_the_automation_here(laptop, failure):
    rt = laptop.runtime
    item = seed(rt)["task"]
    if failure == "down":
        laptop.network.down = True
    elif failure == "token":
        rt.update_preferences({"runner_token": "wrong"})
    else:
        rt.update_preferences({"runner_url": "https://93.184.215.14"})  # Not a Bagley.
    resp = laptop.post(f"/api/automations/{item['id']}/move-to-runner")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    expected = {
        "down": "Can't reach",
        "token": "refused the token",
        "not_bagley": "not Bagley JSON",
    }
    assert expected[failure] in detail
    here = rt.store.get_automation(item["id"])
    assert here["enabled"] and "moved_to" not in here["state"]
    assert laptop.surface.store.list_automations() == []


def test_push_all_and_pull(laptop):
    rt = laptop.runtime
    items = seed(rt)
    assert laptop.post("/api/runner/push", json={}).status_code == 422

    result = laptop.post("/api/runner/push", json={"all": True}).json()
    assert result["ok"] and result["failed"] == []
    assert sorted(m["name"] for m in result["moved"]) == ["Bins", "Laptop price", "Lisbon weather"]
    assert not any(a["enabled"] for a in rt.store.list_automations())
    assert rt.store.get_automation(items["off"]["id"])["state"] == {}  # Disabled ones stay.
    assert len(laptop.surface.store.list_automations()) == 3

    laptop.surface.scheduler.create("task", "Runner only", "hourly", prompt="Check backups")
    pulled = laptop.post("/api/runner/pull").json()
    assert pulled["runner"] == RUNNER_URL
    assert pulled["added"]["automations"] == 1 and pulled["skipped"]["automations"] == 3
    copy = next(a for a in rt.store.list_automations() if a["name"] == "Runner only")
    assert copy["enabled"] is False and copy["state"] == {"runner_copy": RUNNER_URL}

    laptop.network.down = True
    failed = laptop.post("/api/runner/push", json={"ids": [items["off"]["id"]]})
    assert failed.status_code == 502


def test_cli_export_and_import_without_a_server(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BAGLEY_URL", raising=False)
    monkeypatch.setenv("BAGLEY_PORT", str(free_port()))  # Nothing listens: work in-process.
    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "laptop"))
    rt = Runtime(ServerConfig.from_env(), env={})
    seed(rt)
    rt.store.close()
    out = tmp_path / "bagley.json"
    assert cli.main(["export", "--parts", "automations,memories", "-o", str(out)]) == 0
    assert len(json.loads(out.read_text())["automations"]) == 4

    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "surface"))
    capsys.readouterr()
    assert cli.main(["import", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "AUTOMATIONS" in printed and "     4" in printed
    assert cli.main(["import", str(tmp_path / "missing.json")]) == 1

    monkeypatch.setenv("BAGLEY_URL", "http://127.0.0.1:9")  # Pointed elsewhere: no fallback.
    assert cli.main(["export"]) == 1
    assert "NO SIGNAL" in capsys.readouterr().out
