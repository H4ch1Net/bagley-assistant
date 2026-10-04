from __future__ import annotations

import json

import pytest

from bagley.agent import Agent, RunRequest
from bagley.tools import ToolContext, build_registry
from tests.mock_llm import Reply

pytestmark = pytest.mark.anyio


@pytest.fixture
def tools(config):
    config.ensure_dirs()
    return build_registry(config)


async def test_update_plan_reads_checklists(tools, config):
    plan = tools.get("update_plan")
    ctx = ToolContext(config=config, store=None, http=None)
    text, ui = await plan.run(
        {
            "steps": [
                "[x] Find files",
                "[>] Group them",
                "[ ] Move them",
                {"step": "Report", "status": "pending"},
            ]
        },
        ctx,
    )
    assert text == "Plan updated: 1 of 4 done. Next: Group them"
    assert [s["status"] for s in ui["plan"]] == ["done", "doing", "todo", "todo"]
    assert ui["plan"][0]["text"] == "Find files"


async def test_ask_user_waits_for_an_answer(make_runtime, mock, recorder):
    rt = make_runtime()
    asked = []

    async def ask(question, options):
        asked.append((question, options))
        return "Porto"

    mock.script = [
        Reply(
            tool_calls=[("ask_user", {"question": "Which city?", "options": ["Porto", "Lisbon"]})]
        ),
        Reply(text="Porto it is."),
    ]
    await Agent(rt).run(RunRequest(text="weather?", ask=ask), recorder.emit, recorder.approve)
    assert asked == [("Which city?", ["Porto", "Lisbon"])]
    assert recorder.of("tool.end")[0]["result"] == "The user answered: Porto"

    mock.script = [Reply(tool_calls=[("ask_user", {"question": "Which city?"})]), Reply(text="ok")]
    await Agent(rt).run(RunRequest(text="weather?"), recorder.emit, recorder.approve)
    assert recorder.of("tool.end")[-1]["result"].startswith("Nobody can answer right now")


async def test_search_chats_finds_and_reads_earlier_conversations(make_runtime, mock, recorder):
    rt = make_runtime()
    old = rt.store.create_conversation("Tokyo trip")
    rt.store.add_message(old["id"], "user", "Book the flight to Tokyo on March 3rd")
    rt.store.add_message(old["id"], "assistant", "Booked: JL 44, March 3rd, seat 31A.")
    other = rt.store.create_conversation("Bread")
    rt.store.add_message(other["id"], "user", "sourdough ratios please")
    mock.script = [
        Reply(tool_calls=[("search_chats", {"query": "tokyo flight seat"})]),
        Reply(tool_calls=[("search_chats", {"conversation_id": old["id"]})]),
        Reply(text="Seat 31A."),
    ]
    await Agent(rt).run(RunRequest(text="which seat did I get?"), recorder.emit, recorder.approve)
    found, read = (json.loads(e["result"]) for e in recorder.of("tool.end"))
    texts = [r["text"] for r in found["results"]]
    assert texts == ["Book the flight to Tokyo on March 3rd", "Booked: JL 44, March 3rd, seat 31A."]
    assert {r["conversation_id"] for r in found["results"]} == {old["id"]}
    assert read["title"] == "Tokyo trip" and read["messages"][1].startswith("assistant: Booked")


async def test_delegate_task_runs_helpers_with_fresh_context(make_runtime, mock, recorder):
    rt = make_runtime()
    mock.script = [
        Reply(
            tool_calls=[
                (
                    "delegate_task",
                    {"tasks": ["Price of A", "Price of B"], "context": "Compare laptops."},
                )
            ]
        ),
        Reply(text="Helper answer one."),
        Reply(text="Helper answer two."),
        Reply(text="A is cheaper."),
    ]
    await Agent(rt).run(RunRequest(text="compare A and B"), recorder.emit, recorder.approve)
    end = recorder.of("tool.end")[0]
    answers = {r["answer"] for r in json.loads(end["result"])["results"]}
    assert answers == {"Helper answer one.", "Helper answer two."}
    parent = recorder.of("conversation")[0]["conversation"]["id"]
    children = [rt.store.get_conversation(t["conversation_id"]) for t in end["ui"]["subtasks"]]
    assert {c["parent_id"] for c in children} == {parent}
    # Helpers are hidden from the sidebar and from chat search.
    assert [c["id"] for c in rt.store.list_conversations()] == [parent]
    assert rt.store.search_messages("helper answer") == []
    helper_requests = [
        r for r in mock.requests if "helper working on one subtask" in str(r["messages"])
    ]
    assert len(helper_requests) == 2
    for r in helper_requests:
        offered = {t["function"]["name"] for t in r.get("tools", [])}
        assert "delegate_task" not in offered and "ask_user" not in offered
        assert "Background: Compare laptops." in r["messages"][-1]["content"]


async def test_helpers_cannot_use_tools_that_need_approval(make_runtime, mock, recorder):
    rt = make_runtime()
    mock.script = [
        Reply(tool_calls=[("delegate_task", {"tasks": ["Write notes.md"]})]),
        Reply(tool_calls=[("write_file", {"path": "notes.md", "content": "x"})]),
        Reply(text="Could not write."),
        Reply(text="The helper couldn't write it."),
    ]
    await Agent(rt).run(RunRequest(text="delegate a write"), recorder.emit, recorder.approve)
    assert recorder.asked == [] and not (rt.config.workspace / "notes.md").exists()
    child = recorder.of("tool.end")[0]["ui"]["subtasks"][0]["conversation_id"]
    tool_result = next(m for m in rt.store.list_messages(child) if m["role"] == "tool")
    assert tool_result["content"].startswith("Not run: it needs approval")
