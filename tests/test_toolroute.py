from __future__ import annotations

import pytest

from bagley.agent import Agent, RunRequest
from bagley.toolroute import select_tools
from bagley.tools import build_registry
from tests.mock_llm import Reply


@pytest.fixture
def tools(config):
    config.ensure_dirs()
    return list(build_registry(config).tools.values())


def names(selected):
    return {t.name for t in selected}


def user(text):
    return {"role": "user", "content": text}


def test_core_tools_plus_loader_for_plain_questions(tools):
    picked = names(select_tools(tools, [user("What's the weather in Porto?")]))
    assert {"get_weather", "web_search", "read_file", "remember", "load_tools"} <= picked
    assert not picked & {"write_file", "set_reminder", "system_status"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remind me tomorrow to call the bank", "set_reminder"),
        ("Rename the report and move it to archive", "move_file"),
        ("Why is my laptop so slow?", "system_status"),
    ],
)
def test_keywords_load_groups(tools, text, expected):
    assert expected in names(select_tools(tools, [user(text)]))


def test_groups_stay_loaded_after_use(tools):
    history = [
        user("save this"),
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "1", "function": {"name": "write_file", "arguments": "{}"}}],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "2",
                    "function": {"name": "load_tools", "arguments": '{"groups": ["automation"]}'},
                }
            ],
        },
        user("thanks, what's 2+2?"),
    ]
    picked = names(select_tools(tools, history))
    assert {"write_file", "set_reminder"} <= picked


def test_non_latin_messages_get_every_tool(tools):
    assert len(select_tools(tools, [user("明天提醒我给妈妈打电话")])) == len(tools)


def test_small_toolsets_are_not_routed(tools):
    assert select_tools(tools[:5], [user("hi")]) == tools[:5]


@pytest.mark.anyio
async def test_model_can_load_tools_mid_run(make_runtime, mock, recorder):
    rt = make_runtime()
    mock.script = [
        Reply(tool_calls=[("load_tools", {"groups": ["system"]})]),
        Reply(tool_calls=[("system_status", {"top": 1})]),
        Reply(text="All good."),
    ]
    await Agent(rt).run(RunRequest(text="How is everything?"), recorder.emit, recorder.approve)
    offered = [{t["function"]["name"] for t in r["tools"]} for r in mock.requests]
    assert "system_status" not in offered[0] and "load_tools" in offered[0]
    assert "system_status" in offered[1]
    results = [e["result"] for e in recorder.of("tool.end")]
    assert results[0].startswith("Loaded system tools") and '"cpu_percent"' in results[1]
