from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bagley.server import create_app

WS = "ws://localhost/api/ws"  # The test client defaults to Host "testserver", which the guard rejects.


@pytest.fixture
def client(make_runtime):
    rt = make_runtime(env={"BAGLEY_PERSONA": "bagley"})
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        c.runtime = rt
        yield c


def chat(ws, **message):
    ws.send_text(json.dumps({"type": "chat", **message}))
    events = []
    while True:
        event = json.loads(ws.receive_text())
        events.append(event)
        if event["type"] == "approval.request":
            ws.send_text(
                json.dumps({"type": "approval", "id": event["call"]["id"], "decision": "allow"})
            )
        if event["type"] == "run.end":
            return events


def test_index_and_static(client):
    page = client.get("/")
    assert page.status_code == 200 and "Bagley" in page.text and "{{version}}" not in page.text
    assert client.get("/static/js/main.js").status_code == 200


def test_health_and_models(client):
    health = client.get("/api/health").json()
    assert health["ok"] and health["provider"] == "ollama" and health["models"] == 3
    assert health["capabilities"]["tools"] is True
    models = client.get("/api/models").json()
    assert models["can_pull"] and {m["name"] for m in models["models"]} >= {"qwen3:8b"}


def test_preferences(client):
    body = client.get("/api/preferences").json()
    assert body["locked"] == ["persona"]
    assert "api_key" not in body["values"] and body["values"]["has_api_key"] is False

    updated = client.put(
        "/api/preferences", json={"temperature": 1.1, "persona": "concise", "api_key": "sk"}
    ).json()
    assert updated["values"]["temperature"] == 1.1
    assert updated["values"]["persona"] == "bagley"  # Locked by the environment.
    assert updated["values"]["has_api_key"] is True

    bad = client.put("/api/preferences", json={"temperature": 5})
    assert bad.status_code == 422 and "temperature" in bad.json()["detail"]


def test_websocket_chat_and_conversation_api(client):
    with client.websocket_connect(WS) as ws:
        events = chat(ws, text="What's the weather in Lisbon?")
    types = [e["type"] for e in events]
    assert types[:2] == ["conversation", "run.start"]
    assert "tool.end" in types and types[-1] == "run.end"
    cid = events[0]["conversation"]["id"]

    listed = client.get("/api/conversations").json()
    assert listed[0]["id"] == cid
    assert client.get("/api/conversations", params={"q": "lisbon"}).json()[0]["id"] == cid
    assert client.get("/api/conversations", params={"q": "zzz"}).json() == []

    detail = client.get(f"/api/conversations/{cid}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]

    assert (
        client.patch(f"/api/conversations/{cid}", json={"title": "Renamed"}).json()["title"]
        == "Renamed"
    )
    export = client.get(f"/api/conversations/{cid}/export")
    assert 'filename="Renamed.md"' in export.headers["content-disposition"]
    assert "Used `get_weather`" in export.text
    client.patch(f"/api/conversations/{cid}", json={"title": "Météo à Lisbonne 天气"})
    unicode_export = client.get(f"/api/conversations/{cid}/export")
    assert unicode_export.status_code == 200
    assert "filename*=UTF-8''M%C3%A9t%C3%A9o" in unicode_export.headers["content-disposition"]
    assert client.get("/api/conversations", params={"q": "Lisbon_"}).json() == []  # "_" is literal

    assert client.delete(f"/api/conversations/{cid}").status_code == 204
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.post(f"/api/conversations/{cid}/restore").status_code == 200
    assert client.get(f"/api/conversations/{cid}").status_code == 200


def test_websocket_approval_flow(client):
    with client.websocket_connect(WS) as ws:
        events = chat(ws, text="Write a note about the trip")
    assert any(e["type"] == "approval.request" for e in events)
    assert (client.runtime.config.workspace / "trip" / "lisbon.md").exists()


def test_websocket_rejects_concurrent_runs_and_bad_json(client):
    with client.websocket_connect(WS) as ws:
        ws.send_text("not json")
        assert json.loads(ws.receive_text())["message"] == "Malformed message."
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"


def test_memories_and_tools(client):
    created = client.post("/api/memories", json={"content": "Uses metric"}).json()
    assert client.get("/api/memories").json()[0]["content"] == "Uses metric"
    assert client.delete(f"/api/memories/{created['id']}").status_code == 204
    assert client.delete(f"/api/memories/{created['id']}").status_code == 404

    tools = client.get("/api/tools").json()
    names = {t["name"] for t in tools["tools"]}
    assert {"web_search", "write_file", "remember"} <= names
    assert "run_command" not in names


def test_upload_into_workspace(client):
    first = client.put("/api/workspace/uploads/notes.txt", content=b"hello")
    assert first.status_code == 201 and first.json() == {"path": "uploads/notes.txt", "size": 5}
    second = client.put("/api/workspace/uploads/notes.txt", content=b"again")
    assert second.json()["path"] == "uploads/notes-2.txt"
    assert (
        client.put("/api/workspace/uploads/..%2F..%2Fescape.txt", content=b"x").status_code == 404
    )
    assert (
        client.put("/api/workspace/uploads/..evil$.txt", content=b"x").json()["path"]
        == "uploads/evil_.txt"
    )
    assert (client.runtime.config.workspace / "uploads" / "notes.txt").read_text() == "hello"
    too_big = client.put("/api/workspace/uploads/big.bin", content=b"0" * 5_000_001)
    assert too_big.status_code == 413


def test_model_pull_streams_progress(client):
    with client.stream("POST", "/api/models/pull", json={"name": "tiny:1b"}) as resp:
        lines = [json.loads(line) for line in resp.iter_lines() if line]
    assert lines[-1]["status"] == "success"
    assert any(line.get("completed") == 500 for line in lines)


def test_guard_blocks_rebinding_and_cross_origin(client):
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    blocked = client.put("/api/preferences", json={}, headers={"origin": "http://evil.example"})
    assert blocked.status_code == 403
    allowed = client.put("/api/preferences", json={}, headers={"origin": "http://localhost"})
    assert allowed.status_code == 200
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(WS, headers={"origin": "http://evil.example"}),
    ):
        pass


def test_token_auth(make_runtime):
    rt = make_runtime()
    rt.config.token = "s3cret"
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        assert c.get("/api/health").status_code == 401
        login = c.get("/?token=s3cret", follow_redirects=False)
        assert login.status_code == 303 and "bagley_token=s3cret" in login.headers["set-cookie"]
        assert c.get("/api/health").status_code == 200  # Cookie now set.
        c.cookies.clear()
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"authorization": "Bearer s3cret"}).status_code == 200
