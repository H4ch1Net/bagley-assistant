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
