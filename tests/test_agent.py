from __future__ import annotations

import asyncio

import pytest

from bagley.agent import Agent, RunRequest, estimate_tokens, fit_history, reply_text
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


async def test_rejected_runs_still_end(make_runtime, recorder):
    rt = make_runtime()
    await run(rt, recorder, "   ")
    await run(rt, recorder, conversation_id="missing", mode="regenerate")
    assert recorder.types() == ["error", "run.end", "error", "run.end"]
    assert rt.store.list_conversations() == []


async def test_one_run_per_conversation(make_runtime, recorder, mock):
    rt = make_runtime()
    await run(rt, recorder, "hi")
    cid = recorder.of("conversation")[0]["conversation"]["id"]
    rt.busy.add(cid)  # As if another window were answering.
    recorder.events.clear()
    await run(rt, recorder, "again", conversation_id=cid)
    assert recorder.of("error")[0]["message"].startswith("This chat is already answering")
    assert len(rt.store.list_messages(cid)) == 2
    rt.busy.clear()
    await run(rt, recorder, "again", conversation_id=cid)
    assert not rt.busy


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
    rt.learn_think("qwen3:8b", always=False)  # Known to honor "no reasoning": streams at once.

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


async def test_model_that_reasons_anyway_is_caught_and_remembered(make_runtime, recorder, mock):
    # Qwen3 thinking models reason with think: false and print only the closing tag.
    rt = make_runtime()
    mock.script = [
        Reply(text="Okay, the user wants their specs.</think>\n\nHere they are."),
        Reply(reasoning="Short.", text="Second answer."),
    ]
    await run(rt, recorder, "specs?")
    assert mock.requests[0]["think"] is False
    assert recorder.of("text.retract") == []  # Held back until the model showed its kind.
    assert recorder.text() == "Here they are."
    assert "".join(e["text"] for e in recorder.of("reasoning.delta")) == (
        "Okay, the user wants their specs."
    )
    cid = recorder.of("run.end")[0]["conversation_id"]
    reply = rt.store.list_messages(cid)[-1]
    assert reply["content"] == "Here they are."
    assert reply["reasoning"] == "Okay, the user wants their specs."
    assert "qwen3:8b" in rt.always_think

    await run(rt, recorder, "again", conversation_id=cid)
    assert mock.requests[-1]["think"] is True  # Let the server split the reasoning off.
    # Remembered across restarts.
    assert "qwen3:8b" in make_runtime().always_think


async def test_model_that_honors_think_off_streams_after_its_first_reply(
    make_runtime, recorder, mock
):
    rt = make_runtime()
    mock.script = [Reply(text="First answer in a few words."), Reply(text="Second answer too.")]
    await run(rt, recorder, "one")
    assert len(recorder.of("text.delta")) == 1  # The first reply comes in one piece.
    assert rt.think_checked == {"qwen3:8b"} and not rt.always_think
    recorder.events.clear()
    cid = rt.store.list_conversations()[0]["id"]
    await run(rt, recorder, "two", conversation_id=cid)
    assert len(recorder.of("text.delta")) > 1
    assert mock.requests[-1]["think"] is False


async def test_lone_think_tag_after_streaming_retracts_the_text(make_runtime, recorder, mock):
    rt = make_runtime()
    rt.learn_think("qwen3:8b", always=False)
    mock.script = [Reply(text="Let me see what they want.</think>\n\nThe answer.")]
    await run(rt, recorder, "q")
    retract = recorder.of("text.retract")
    before = recorder.events[: recorder.events.index(retract[0])]
    sent = "".join(e["text"] for e in before if e["type"] == "text.delta")
    assert len(retract) == 1 and retract[0]["chars"] == len(sent) > 0
    assert reply_text(recorder.events) == "The answer."
    assert "qwen3:8b" in rt.always_think


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


def test_fit_history_keeps_current_turn_when_tool_results_are_huge():
    call = lambda i: {"id": f"c{i}", "function": {"name": "fetch_webpage", "arguments": "{}"}}  # noqa: E731
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "Compare these two pages"},
        {"role": "assistant", "content": "", "tool_calls": [call(1)]},
        {"role": "tool", "content": "a" * 12_000, "tool_call_id": "c1", "name": "fetch_webpage"},
        {"role": "assistant", "content": "", "tool_calls": [call(2)]},
        {"role": "tool", "content": "b" * 12_000, "tool_call_id": "c2", "name": "fetch_webpage"},
    ]
    out = fit_history(history, budget=3000)
    roles = [m["role"] for m in out]
    assert roles[-5:] == ["user", "assistant", "tool", "assistant", "tool"]
    assert out[-5]["content"] == "Compare these two pages"
    assert all("trimmed" in m["content"] for m in out if m["role"] == "tool")
    assert sum(estimate_tokens(m) for m in out) <= 3000


def test_fit_history_drops_interleaved_results():
    call = {"id": "x", "function": {"name": "calculate", "arguments": "{}"}}
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "user", "content": "q2"},
        {"role": "tool", "content": "late", "tool_call_id": "x", "name": "calculate"},
    ]
    assert [m["role"] for m in fit_history(history, 10_000)] == ["user", "user"]


def test_vllm_without_tool_parser_triggers_text_mode():
    from bagley.llm import ToolsUnsupportedError
    from bagley.llm.openai import OpenAIProvider

    err = OpenAIProvider._http_error(
        400,
        '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set',
        "m",
    )
    assert isinstance(err, ToolsUnsupportedError)


async def test_notify_marks_unread_without_open_windows(make_runtime, recorder, mock):
    rt = make_runtime()
    mock.script = [
        Reply(tool_calls=[("notify_user", {"title": "Done", "message": "All set"})]),
        Reply(text="ok"),
    ]
    await run(rt, recorder, "tell me when done")
    cid = recorder.of("conversation")[0]["conversation"]["id"]
    assert "marked unread" in recorder.of("tool.end")[0]["result"]
    assert rt.store.get_conversation(cid)["unread"] == 1
