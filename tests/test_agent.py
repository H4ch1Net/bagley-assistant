from __future__ import annotations

import asyncio

import pytest

from bagley.agent import Agent, RunRequest, fit_history
from tests.mock_llm import Reply

pytestmark = pytest.mark.anyio


async def run(rt, recorder, text="", **kwargs):
    await Agent(rt).run(RunRequest(text=text, **kwargs), recorder.emit, recorder.approve)
    return recorder


async def test_native_tool_call_round_trip(make_runtime, recorder):
    rt = make_runtime()
    await run(rt, recorder, "What's the weather in Lisbon this weekend?")

    types = recorder.types()
    assert types[0] == "conversation"
    assert "tool.start" in types and "tool.end" in types
    assert types[-1] == "run.end"
    tool_end = recorder.of("tool.end")[0]
    assert tool_end["ok"] is True
    assert "Lisbon" in tool_end["result"]
    assert "22°C" in recorder.text()

    cid = recorder.of("conversation")[0]["conversation"]["id"]
    roles = [m["role"] for m in rt.store.list_messages(cid)]
    assert roles == ["user", "assistant", "tool", "assistant"]
    first = rt.store.list_messages(cid)[1]
    assert first["tool_calls"][0]["function"]["name"] == "get_weather"
    assert first["reasoning"]  # Thinking text from the mock is kept separately.
    stats = recorder.of("run.end")[0]["stats"]
    assert stats["tokens_per_second"] == pytest.approx(42, rel=0.1)


async def test_openai_provider_assembles_streamed_tool_arguments(make_runtime, recorder, mock):
    rt = make_runtime(provider="openai", model="llama3.2:3b")
    await run(rt, recorder, "What's 12% tip on 128.68 split 4 ways?")
    start = recorder.of("tool.start")[0]
    assert start["call"]["name"] == "calculate"
    assert start["call"]["arguments"] == {"expression": "128.68 * 1.12 / 4"}
    assert recorder.of("tool.end")[0]["result"].endswith("= 36.0304")
    assert mock.requests[0]["tools"]


async def test_prompt_mode_for_models_without_tool_support(make_runtime, recorder, mock):
    rt = make_runtime(model="gemma2:2b")
    await run(rt, recorder, "Please remember I like metric units")
    assert recorder.of("model")[0]["tool_mode"] == "prompt"
    assert "tools" not in mock.requests[0]
    assert "<tool_call>" in mock.requests[0]["messages"][0]["content"]
    assert recorder.of("tool.end")[0]["ok"]
    assert rt.store.list_memories()[0]["content"].startswith("Prefers metric")
    # The follow-up request carries the result as a <tool_response> user message.
    assert "<tool_response" in mock.requests[1]["messages"][-1]["content"]


async def test_falls_back_to_prompt_mode_when_server_rejects_tools(make_runtime, recorder, mock):
    rt = make_runtime(provider="openai", model="gemma2:2b")
    await run(rt, recorder, "Please remember I like metric units")
    assert any("no native tool support" in e["message"] for e in recorder.of("notice"))
    assert recorder.of("tool.end")[0]["ok"]
    assert "gemma2:2b" in rt.prompt_mode_models


async def test_text_tool_calls_are_parsed_in_native_mode(make_runtime, recorder, mock):
    mock.script = [
        Reply(tool_calls=[("calculate", {"expression": "6*7"})], text_tool_call=True),
        Reply(text="42."),
    ]
    rt = make_runtime()
    await run(rt, recorder, "six times seven")
    assert recorder.of("tool.end")[0]["result"] == "6*7 = 42"
    assert "<tool_call>" not in recorder.text()


async def test_confirm_tools_ask_and_respect_denial(make_runtime, recorder):
    recorder.decision = False
    rt = make_runtime()
    await run(rt, recorder, "Save a note about the trip")
    assert recorder.asked == ["write_file"]
    assert recorder.of("approval.result")[0]["allowed"] is False
    assert not (rt.config.workspace / "trip" / "lisbon.md").exists()
    assert "declined" in recorder.of("tool.end")[0]["result"]


async def test_confirm_tools_run_when_allowed(make_runtime, recorder):
    rt = make_runtime()
    await run(rt, recorder, "Save a note about the trip")
    assert (rt.config.workspace / "trip" / "lisbon.md").read_text().startswith("# Lisbon")


async def test_disabled_tools_are_not_offered(make_runtime, recorder, mock):
    rt = make_runtime(disabled_tools=["get_weather"])
    mock.script = [Reply(tool_calls=[("get_weather", {"location": "x"})]), Reply(text="ok")]
    await run(rt, recorder, "weather?")
    offered = {t["function"]["name"] for t in mock.requests[0]["tools"]}
    assert "get_weather" not in offered
    assert recorder.of("tool.end")[0]["ok"] is False


async def test_missing_model_reports_hint_and_regenerate_recovers(make_runtime, recorder):
    rt = make_runtime(model="nope:1b")
    await run(rt, recorder, "hello")
    error = recorder.of("error")[0]
    assert "not installed" in error["message"]
    assert "ollama pull nope:1b" in error["hint"]

    cid = recorder.of("conversation")[0]["conversation"]["id"]
    rt.store.set_preferences({"model": "llama3.2:3b"})
    recorder.events.clear()
    await run(rt, recorder, conversation_id=cid, mode="regenerate")
    assert not recorder.of("error")
    assert [m["role"] for m in rt.store.list_messages(cid)] == ["user", "assistant"]


async def test_edit_replaces_last_exchange(make_runtime, recorder, mock):
    rt = make_runtime()
    await run(rt, recorder, "hi")
    cid = recorder.of("conversation")[0]["conversation"]["id"]
    mock.script = [Reply(text="Edited answer")]
    await run(rt, recorder, "hello there", conversation_id=cid, mode="edit")
    messages = rt.store.list_messages(cid)
    assert [m["content"] for m in messages] == ["hello there", "Edited answer"]


async def test_cancel_keeps_partial_reply(make_runtime, recorder, mock):
    mock.script = [Reply(text="word " * 200)]
    rt = make_runtime()

    async def emit(event):
        await recorder.emit(event)
        if len(recorder.of("text.delta")) == 3:
            task.cancel()

    task = asyncio.create_task(Agent(rt).run(RunRequest(text="talk"), emit, recorder.approve))
    await task
    end = recorder.of("run.end")[0]
    assert end["stopped"] is True
    cid = end["conversation_id"]
    last = rt.store.list_messages(cid)[-1]
    assert last["meta"]["interrupted"] is True
    assert last["content"].startswith("word")


async def test_step_limit(make_runtime, recorder, mock):
    rt = make_runtime(max_steps=2)
    mock.script = [Reply(tool_calls=[("calculate", {"expression": "1+1"})]) for _ in range(5)]
    await run(rt, recorder, "loop")
    assert len(recorder.of("tool.end")) == 2
    assert "Stopped after 2 steps" in recorder.of("notice")[-1]["message"]


async def test_reasoning_streams_separately(make_runtime, recorder, mock):
    rt = make_runtime(think=True)
    mock.script = [Reply(reasoning="Let me think.", text="Answer.")]
    await run(rt, recorder, "q")
    assert "".join(e["text"] for e in recorder.of("reasoning.delta")) == "Let me think."
    assert recorder.text() == "Answer."
    assert mock.requests[0]["think"] is True


async def test_smart_title(make_runtime, recorder):
    rt = make_runtime(smart_titles=True)
    await run(rt, recorder, "What's the weather like in Lisbon?")
    for _ in range(50):
        if recorder.of("title"):
            break
        await asyncio.sleep(0.01)
    assert recorder.of("title")[0]["title"] == "Weekend weather in Lisbon"


def test_fit_history_drops_orphans_and_starts_with_user():
    history = [
        {"role": "assistant", "content": "orphan"},
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "a", "function": {"name": "x", "arguments": "{}"}}],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "b", "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "r", "tool_call_id": "b", "name": "x"},
        {"role": "tool", "content": "stray", "tool_call_id": "zzz", "name": "x"},
    ]
    out = fit_history(history, budget=10_000)
    assert [m["role"] for m in out] == ["user", "assistant", "tool"]
    assert out[1]["tool_calls"][0]["id"] == "b"


def test_fit_history_respects_budget():
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 300} for i in range(20)
    ]
    out = fit_history(history, budget=400)
    assert 1 <= len(out) < 20
    assert out[0]["role"] == "user"
