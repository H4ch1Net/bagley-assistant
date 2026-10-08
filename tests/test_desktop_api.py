"""The API for clients outside the browser, shared approvals, permission tiers and modes."""

from __future__ import annotations

import base64
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from bagley import notify
from bagley.agent import Agent, RunRequest
from bagley.server import create_app
from tests.mock_llm import Reply

pytestmark = pytest.mark.anyio

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


@pytest.fixture
def client(make_runtime, mock):
    rt = make_runtime()
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        c.runtime = rt
        c.mock = mock
        yield c


def ask(client, **body):
    with client.stream("POST", "/api/ask", json=body) as resp:
        assert resp.status_code == 200, resp.read()
        return [json.loads(line) for line in resp.iter_lines() if line]


def test_ask_streams_a_turn_with_context(client):
    client.mock.script = [Reply(text="It's a missing semicolon.")]
    events = ask(
        client,
        text="What's this error?",
        source="overlay",
        context={"window_title": "kitty — cargo build", "selection": "error[E0308]"},
    )
    types = [e["type"] for e in events]
    assert types[0] == "conversation" and types[-1] == "run.end"
    assert "".join(e["text"] for e in events if e["type"] == "text.delta") == (
        "It's a missing semicolon."
    )
    sent = client.mock.requests[0]["messages"][-1]["content"]
    assert sent.startswith("What's this error?\n\n<context>")
    assert "Focused window: kitty — cargo build" in sent and "error[E0308]" in sent
    cid = events[-1]["conversation_id"]
    user = client.runtime.store.list_messages(cid)[0]
    assert user["meta"]["source"] == "overlay"
    assert user["meta"]["context"]["selection"] == "error[E0308]"


def test_ask_with_an_image_stores_a_capture(client):
    client.mock.script = [Reply(text="A cat.")]
    events = ask(client, text="What is this?", images=[{"data": base64.b64encode(PNG).decode()}])
    cid = events[-1]["conversation_id"]
    name = client.runtime.store.list_messages(cid)[0]["meta"]["images"][0]
    served = client.get(f"/api/captures/{name}")
    assert served.status_code == 200 and served.content == PNG
    assert client.get("/api/captures/..%2Fbagley.db").status_code == 404
    bad = client.post("/api/ask", json={"text": "x", "images": [{"data": "bm90IGFuIGltYWdl"}]})
    assert bad.status_code == 415


def test_upload_capture_for_the_browser(client):
    up = client.post("/api/captures", content=PNG)
    assert up.status_code == 201 and up.json()["url"].startswith("/api/captures/")
    assert client.post("/api/captures", content=b"plain text").status_code == 415


def test_approvals_can_be_answered_from_another_client(client):
    client.mock.script = [
        Reply(tool_calls=[("write_file", {"path": "note.md", "content": "hi"})]),
        Reply(text="Saved."),
    ]
    events: list[dict] = []
    worker = threading.Thread(
        target=lambda: events.extend(ask(client, text="save a note", source="overlay"))
    )
    worker.start()
    for _ in range(200):
        pending = client.get("/api/approvals").json()
        if pending:
            break
        threading.Event().wait(0.02)
    assert pending[0]["tool"] == "write_file" and pending[0]["source"] == "overlay"
    assert pending[0]["summary"].startswith("Write")
    assert client.get("/api/activity").json()["code"] == "AWAIT"
    assert client.post(f"/api/approvals/{pending[0]['id']}", json={"decision": "allow"}).json()
    worker.join(10)
    assert (client.runtime.config.workspace / "note.md").read_text() == "hi"
    audit = client.get("/api/audit").json()[0]
    assert audit["tool"] == "write_file" and audit["decision"] == "approved"
    assert audit["source"] == "overlay" and audit["ok"] is True
    assert (
        client.post(f"/api/approvals/{pending[0]['id']}", json={"decision": "allow"}).status_code
        == 404
    )


def test_ask_can_refuse_approvals_up_front(client):
    client.mock.script = [
        Reply(tool_calls=[("write_file", {"path": "x.md", "content": "x"})]),
        Reply(text="I couldn't save it."),
    ]
    events = ask(client, text="save", approvals="deny")
    assert any(e["type"] == "approval.result" and not e["allowed"] for e in events)
    assert not (client.runtime.config.workspace / "x.md").exists()
    assert client.get("/api/audit").json()[0]["decision"] == "denied"


def test_activity_stream_and_status(client):
    snap = client.get("/api/activity").json()
    assert snap["state"] == "idle" and snap["code"] == "IDLE"
    assert snap["machine"] == ""


async def test_permission_tiers(make_runtime, recorder, mock):
    rt = make_runtime(tool_permissions={"write_file": "allow", "get_weather": "deny"})
    mock.script = [
        Reply(tool_calls=[("write_file", {"path": "auto.md", "content": "ok"})]),
        Reply(text="Done."),
    ]
    await Agent(rt).run(RunRequest(text="save it"), recorder.emit, recorder.approve)
    assert recorder.asked == []  # Allowed without asking.
    assert (rt.config.workspace / "auto.md").read_text() == "ok"
    assert rt.store.list_audit()[0]["decision"] == "auto"
    offered = {t["function"]["name"] for t in mock.requests[0]["tools"]}
    assert "get_weather" not in offered and "write_file" in offered


async def test_unattended_runs_ignore_allow_for_risky_tools(make_runtime, mock):
    rt = make_runtime(tool_permissions={"write_file": "allow"})
    mock.script = [
        Reply(tool_calls=[("write_file", {"path": "x.md", "content": "x"})]),
        Reply(text="Couldn't."),
    ]
    item = rt.scheduler.create("task", "Write", "every 2 hours", "write x")
    await rt.scheduler.run(item)
    assert not (rt.config.workspace / "x.md").exists()
    assert rt.store.list_audit()[0]["decision"] == "blocked"


async def test_modes_shape_the_prompt(make_runtime, recorder, mock):
    rt = make_runtime(work_name="ATS", work_languages=["English", "French"])
    mock.script = [Reply(text="Summary ready.")]
    request = RunRequest(text="Summarise ticket 42", conversation_mode="work")
    await Agent(rt).run(request, recorder.emit, recorder.approve)
    system = mock.requests[0]["messages"][0]["content"]
    assert "# Work mode (ATS)" in system and "English and French" in system
    conv = recorder.of("conversation")[0]["conversation"]
    assert conv["mode"] == "work"


def test_mode_can_change_and_is_listed(client):
    cid = client.runtime.store.create_conversation("x")["id"]
    changed = client.patch(f"/api/conversations/{cid}", json={"mode": "study"}).json()
    assert changed["mode"] == "study" and changed["title"] == "x"
    assert client.patch(f"/api/conversations/{cid}", json={"mode": "nope"}).status_code == 422
    modes = client.get("/api/preferences").json()["modes"]
    assert [m["id"] for m in modes] == ["default", "study", "cybersec", "work"]
    assert client.get("/api/modes").json()[3]["label"] == "ATS"


def test_tools_report_permissions(client):
    client.runtime.update_preferences({"tool_permissions": {"write_file": "allow"}})
    tools = {t["name"]: t for t in client.get("/api/tools").json()["tools"]}
    assert tools["write_file"]["permission"] == "allow"
    assert tools["write_file"]["default_permission"] == "ask"
    assert tools["get_weather"]["permission"] == "allow"


def test_secret_preferences_stay_hidden(client):
    client.put(
        "/api/preferences",
        json={
            "ntfy_token": "tk_secret",
            "machines": [
                {"id": "gpu", "name": "H4CH1", "base_url": "http://gpu:11434", "api_key": "k"}
            ],
        },
    )
    body = client.get("/api/preferences").json()
    values = body["values"]
    assert "ntfy_token" not in values and values["has_ntfy_token"] is True
    assert values["machines"][0]["has_api_key"] is True and "api_key" not in values["machines"][0]
    assert body["machine"]


# Notifications ---------------------------------------------------------------------------------


async def test_ntfy_gets_important_notifications(make_runtime, monkeypatch):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "x"})

    rt = make_runtime(ntfy_url="https://ntfy.example/bagley-test", ntfy_token="tk")
    rt.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(notify, "desktop_available", lambda: False)
    await rt.notify("Reminder", "Stretch")  # info: below the "important" threshold
    await rt.notify("MORNING BRIEFING", "2 WARN · sshd: 4 failed logins", level="important")
    for task in list(rt._notify_tasks):
        await task
    assert len(sent) == 1
    request = sent[0]
    assert request.url == "https://ntfy.example/bagley-test"
    assert request.headers["Title"] == "MORNING BRIEFING"
    assert request.headers["Priority"] == "high"
    assert request.headers["Authorization"] == "Bearer tk"
    assert request.content == b"2 WARN \xc2\xb7 sshd: 4 failed logins"
    await rt.http.aclose()


async def test_desktop_notifications_use_notify_send(make_runtime, monkeypatch):
    calls: list[tuple] = []

    async def fake_send(note, *, wait=0):
        calls.append((note.title, note.body, note.level))
        return None

    rt = make_runtime(desktop_notifications=True)
    monkeypatch.setattr(notify, "desktop_available", lambda: True)
    monkeypatch.setattr(notify, "send_desktop", fake_send)
    await rt.notify("Service failed", "nginx.service", level="critical")
    for task in list(rt._notify_tasks):
        await task
    assert calls == [("Service failed", "nginx.service", "critical")]


def test_ntfy_headers_encode_unicode_titles():
    headers = notify.ntfy_headers(notify.Note("Café ☕", "x", level="critical", url="http://b/"))
    assert headers["Title"].startswith("=?UTF-8?B?")
    assert headers["Priority"] == "urgent" and headers["Click"] == "http://b/"
