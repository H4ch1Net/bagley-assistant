"""Claude through the official SDK, against a fake Messages API on a mock transport."""

from __future__ import annotations

import json
from typing import Any

import pytest

from bagley.agent import Agent, RunRequest, public_message
from bagley.llm import (
    ClaudeProvider,
    LLMError,
    UnreachableError,
    create_provider,
    detect_kind,
    key_from_env,
)
from bagley.llm.claude import BINDING_BETA, FALLBACK_BETA, UPDATES_BETA, after_fallback, to_wire

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

pytestmark = pytest.mark.anyio

SIGNATURE = "sig-" + "x" * 40


def sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def reply(
    *,
    text: str = "",
    thinking: str | None = None,
    tools: tuple[tuple[str, str, dict[str, Any]], ...] = (),
    stop: str | None = None,
    category: str | None = None,
    model: str = "claude-opus-5-5",
    fallback_after_text: str = "",
) -> list[dict[str, Any]]:
    """The stream events of one reply."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 30},
            },
        }
    ]
    index = 0

    def block(start: dict[str, Any], *deltas: dict[str, Any]) -> None:
        nonlocal index
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": d} for d in deltas)
        events.append({"type": "content_block_stop", "index": index})
        index += 1

    if fallback_after_text:
        block(
            {"type": "thinking", "thinking": "", "signature": ""},
            {"type": "signature_delta", "signature": "declined"},
        )
        block({"type": "text", "text": ""}, {"type": "text_delta", "text": fallback_after_text})
        block({"type": "fallback", "from": {"model": model}, "to": {"model": "claude-opus-4-8"}})
    if thinking is not None:
        block(
            {"type": "thinking", "thinking": "", "signature": ""},
            {"type": "thinking_delta", "thinking": thinking},
            {"type": "signature_delta", "signature": SIGNATURE},
        )
    if text:
        half = len(text) // 2
        block(
            {"type": "text", "text": ""},
            {"type": "text_delta", "text": text[:half]},
            {"type": "text_delta", "text": text[half:]},
        )
    for call_id, name, args in tools:
        raw = json.dumps(args)
        block(
            {"type": "tool_use", "id": call_id, "name": name, "input": {}},
            {"type": "input_json_delta", "partial_json": raw[:3]},
            {"type": "input_json_delta", "partial_json": raw[3:]},
        )
    details = {"type": "refusal", "category": category, "explanation": None} if category else None
    events.append(
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop or ("tool_use" if tools else "end_turn"),
                "stop_sequence": None,
                "stop_details": details,
            },
            "usage": {"output_tokens": 48},
        }
    )
    events.append({"type": "message_stop"})
    return events  # fmt: skip


class FakeClaude:
    """A scripted Messages API. Each POST /v1/messages takes the next reply (or error)."""

    def __init__(self) -> None:
        self.script: list[Any] = []
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.model_headers: list[dict[str, str]] = []

    def handler(self, request: Any) -> Any:
        if request.url.path == "/v1/models":
            self.model_headers.append(dict(request.headers))
            data = [
                {"type": "model", "id": i, "display_name": n, "created_at": "2026-09-01T00:00:00Z"}
                for i, n in (("claude-opus-5-5", "Claude Opus 5.5"), ("claude-haiku-5-5", "Claude Haiku 5.5"))
            ]  # fmt: skip
            return httpx2.Response(
                200, json={"data": data, "has_more": False, "first_id": None, "last_id": None}
            )
        if request.url.path.startswith("/v1/models/"):
            model = request.url.path.rsplit("/", 1)[1]
            body = {
                "type": "model",
                "id": model,
                "display_name": model,
                "created_at": "2026-09-01T00:00:00Z",
                "max_input_tokens": 1_000_000,
            }
            return httpx2.Response(200, json=body)  # fmt: skip
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        step = self.script.pop(0) if self.script else reply(text="Done.")
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):  # (status, message)
            status, message = step
            kind = "invalid_request_error" if status == 400 else "api_error"
            return httpx2.Response(
                status, json={"type": "error", "error": {"type": kind, "message": message}}
            )
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(step)
        )

    def client(self, **kwargs: Any) -> Any:
        transport = httpx2.MockTransport(self.handler)
        return anthropic.AsyncAnthropic(
            api_key=kwargs.get("api_key") or "sk-ant-test",
            base_url=kwargs.get("base_url") or "https://api.anthropic.com",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=transport),
        )


@pytest.fixture
def fake(monkeypatch) -> FakeClaude:
    """Every ClaudeProvider the app creates talks to the fake API."""
    api = FakeClaude()
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _patched(api, anthropic.AsyncAnthropic))
    return api


def _patched(api: FakeClaude, real: Any) -> Any:
    def make(**kwargs: Any) -> Any:
        transport = httpx2.MockTransport(api.handler)
        return real(**{**kwargs, "max_retries": 0, "http_client": anthropic.DefaultAsyncHttpxClient(transport=transport)})  # fmt: skip

    return make


async def collect(
    provider: ClaudeProvider, messages: list[dict[str, Any]], **kwargs: Any
) -> list[Any]:
    return [c async for c in provider.chat(messages, **kwargs)]  # fmt: skip


HISTORY = [
    {"role": "system", "content": "You are Bagley."},
    {"role": "user", "content": "Weather in Porto?", "images": ["iVBORw0KGgo="]},
]


# Conversion -----------------------------------------------------------------------------------


def test_history_becomes_messages_api_blocks():
    calls = [
        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Porto"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "get_current_time", "arguments": "{}"}},
    ]  # fmt: skip
    system, wire = to_wire(
        [
            *HISTORY,
            {"role": "assistant", "content": "Checking.", "tool_calls": calls},
            {"role": "tool", "tool_call_id": "call_1", "content": "18°C"},
            {"role": "tool", "tool_call_id": "call_2", "content": ""},
            {"role": "system", "content": "Be brief."},
        ]
    )
    assert system == "You are Bagley."
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    image, text = wire[0]["content"]
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}
    assert text == {"type": "text", "text": "Weather in Porto?"}
    assert wire[1]["content"][1] == {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Porto"}}  # fmt: skip
    results = wire[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text"]
    assert results[1]["content"] == "(no output)"
    assert "<system-note>" in results[2]["text"]


def test_native_replies_replay_unchanged_or_without_thinking():
    native = {
        "kind": "anthropic",
        "content": [
            {"type": "thinking", "thinking": "", "signature": SIGNATURE},
            {"type": "text", "text": ""},
            {"type": "tool_use", "id": "toolu_1", "name": "get_current_time", "input": {}},
        ],
    }
    message = {"role": "assistant", "content": "", "tool_calls": [], "native": native}
    _, wire = to_wire([{"role": "user", "content": "time?"}, message])
    assert [b["type"] for b in wire[1]["content"]] == ["thinking", "tool_use"]
    assert wire[1]["content"][0]["signature"] == SIGNATURE
    _, plain = to_wire([{"role": "user", "content": "time?"}, message], keep_thinking=False)
    assert [b["type"] for b in plain[1]["content"]] == ["tool_use"]
    # Another provider's native data is ignored.
    other = {**message, "native": {"kind": "other", "content": []}, "content": "hi"}
    _, wire = to_wire([{"role": "user", "content": "x"}, other])
    assert wire[1]["content"] == [{"type": "text", "text": "hi"}]


def test_after_a_fallback_only_text_before_the_boundary_is_kept():
    content = [
        {"type": "thinking", "thinking": "", "signature": "a"},
        {"type": "text", "text": "Partial "},
        {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "", "signature": "b"},
        {"type": "text", "text": "answer"},
    ]
    assert after_fallback(content) == [content[1], content[4], content[5]]
    assert after_fallback(content[4:]) == content[4:]


def test_provider_kind_and_keys_from_the_environment():
    env = {"ANTHROPIC_API_KEY": "sk-ant", "OPENROUTER_API_KEY": "sk-or"}
    assert key_from_env("anthropic", "https://api.anthropic.com", env) == "sk-ant"
    assert key_from_env("anthropic", "https://claude-proxy.internal", env) == "sk-ant"
    assert key_from_env("openai", "https://openrouter.ai/api/v1", env) == "sk-or"
    assert key_from_env("openai", "http://h4ch1:1234/v1", env) == ""


async def test_detects_and_creates_the_claude_provider():
    assert await detect_kind("https://api.anthropic.com") == "anthropic"
    provider = await create_provider(
        "auto", "https://api.anthropic.com", env={"ANTHROPIC_API_KEY": "k"}
    )
    assert isinstance(provider, ClaudeProvider) and provider.api_key == "k"
    await provider.aclose()


# Requests -------------------------------------------------------------------------------------


def test_request_settings_follow_the_model():
    provider = ClaudeProvider("https://api.anthropic.com", "k")
    opus = provider.request(HISTORY, "claude-opus-5-5")
    assert opus["thinking"] == {"type": "adaptive", "display": "updates", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}  # fmt: skip
    assert opus["betas"] == [BINDING_BETA, UPDATES_BETA, FALLBACK_BETA]
    assert opus["fallbacks"] == "default"
    assert opus["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in opus
    assert opus["system"] == "You are Bagley."

    thinking = provider.request(HISTORY, "claude-opus-4-8", think=True)
    assert thinking["thinking"]["display"] == "summarized"
    assert thinking["betas"] == [BINDING_BETA]

    older = provider.request(HISTORY, "claude-opus-4-8")
    assert "thinking" not in older and "betas" not in older and "fallbacks" not in older

    haiku = provider.request(HISTORY, "claude-haiku-4-5", think=True)
    assert "thinking" not in haiku  # No adaptive thinking there.

    proxied = ClaudeProvider("https://claude-proxy.internal", "k").request(
        HISTORY, "claude-opus-5-5"
    )
    assert "fallbacks" not in proxied and FALLBACK_BETA not in proxied["betas"]

    plain = provider.request(HISTORY, "claude-opus-5-5", plain=True)
    assert not {"thinking", "betas", "fallbacks"} & plain.keys()


# Streaming ------------------------------------------------------------------------------------


async def test_streams_text_reasoning_and_tool_calls():
    api = FakeClaude()
    api.script = [reply(thinking="Need the time.", text="Let me check.", tools=(("toolu_1", "get_current_time", {"zone": "Asia/Tokyo"}),))]  # fmt: skip
    provider = ClaudeProvider("https://api.anthropic.com", "k", client=api.client())
    chunks = await collect(provider, HISTORY, model="claude-opus-5-5", tools=[{"type": "function", "function": {"name": "get_current_time", "description": "Time", "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}}}}])  # fmt: skip

    assert "".join(c.reasoning for c in chunks) == "Need the time."
    assert "".join(c.text for c in chunks) == "Let me check."
    final = chunks[-1]
    assert [(c.id, c.name, c.arguments) for c in final.tool_calls] == [("toolu_1", "get_current_time", {"zone": "Asia/Tokyo"})]  # fmt: skip
    assert final.usage.prompt_tokens == 42 and final.usage.completion_tokens == 48
    assert final.native["kind"] == "anthropic"
    assert [b["type"] for b in final.native["content"]] == ["thinking", "text", "tool_use"]
    assert final.native["content"][0]["signature"] == SIGNATURE

    sent = api.requests[0]
    assert sent["tools"][0]["input_schema"]["properties"]["zone"] == {"type": "string"}
    assert sent["stream"] is True and sent["fallbacks"] == "default"
    assert set(api.headers[0]["anthropic-beta"].split(",")) == {
        BINDING_BETA,
        UPDATES_BETA,
        FALLBACK_BETA,
    }
    await provider.aclose()


async def test_a_rejected_setup_is_retried_plain_once():
    api = FakeClaude()
    api.script = [
        (400, "thinking.display: Input should be 'summarized' or 'omitted'"),
        reply(text="Hi."),
    ]
    provider = ClaudeProvider("https://api.anthropic.com", "k", client=api.client())
    chunks = await collect(provider, HISTORY, model="claude-opus-5-5")
    assert "".join(c.text for c in chunks) == "Hi."
    assert "thinking" in api.requests[0] and "thinking" not in api.requests[1]
    assert "anthropic-beta" not in api.headers[1]

    api.script = [(400, "messages: bad"), (400, "messages: bad")]
    with pytest.raises(LLMError, match="messages: bad"):
        await collect(provider, HISTORY, model="claude-opus-5-5")
    assert len(api.requests) == 4  # One retry, then the error.


async def test_refusals_errors_and_outages():
    api = FakeClaude()
    provider = ClaudeProvider("https://api.anthropic.com", "k", client=api.client())

    api.script = [reply(stop="refusal", category="cyber")]
    with pytest.raises(LLMError, match=r"declined this request \(cyber\)"):
        await collect(provider, HISTORY, model="claude-opus-5-5")

    api.script = [(401, "invalid x-api-key")]
    with pytest.raises(LLMError, match="rejected the API key") as err:
        await collect(provider, HISTORY, model="claude-opus-4-8")
    assert not isinstance(err.value, UnreachableError)

    api.script = [(529, "Overloaded")]
    with pytest.raises(UnreachableError):  # So routing moves on to the next machine.
        await collect(provider, HISTORY, model="claude-opus-4-8")

    api.script = [httpx2.ConnectError("no route")]
    with pytest.raises(UnreachableError, match="Can't reach the Anthropic API"):
        await collect(provider, HISTORY, model="claude-opus-4-8")

    api.script = [reply(stop="max_tokens", tools=(("toolu_9", "write_file", {"path": "a"}),))]
    with pytest.raises(LLMError, match="cut off"):
        await collect(provider, HISTORY, model="claude-opus-4-8")


async def test_mid_stream_fallback_keeps_text_and_drops_the_declined_thinking():
    api = FakeClaude()
    api.script = [reply(fallback_after_text="Sure, ", text="here it is.")]
    provider = ClaudeProvider("https://api.anthropic.com", "k", client=api.client())
    chunks = await collect(provider, HISTORY, model="claude-opus-5-5")
    assert "".join(c.text for c in chunks) == "Sure, here it is."
    assert [b["type"] for b in chunks[-1].native["content"]] == ["text", "text"]


async def test_lists_models_and_reads_the_context_window():
    api = FakeClaude()
    provider = ClaudeProvider("https://api.anthropic.com", "k", client=api.client())
    assert [m.name for m in await provider.list_models()] == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert await provider.version() == "anthropic"
    caps = await provider.capabilities("claude-opus-5-5")
    assert caps.tools and caps.vision and caps.thinking and caps.context_length == 1_000_000
    assert not (await provider.capabilities("claude-haiku-4-5")).thinking


# The agent ------------------------------------------------------------------------------------


async def test_agent_replays_thinking_within_the_turn_only(make_runtime, recorder, fake):
    rt = make_runtime(provider="anthropic", base_url="https://api.anthropic.com", api_key="sk-ant-x", model="claude-opus-5-5")  # fmt: skip
    fake.script = [
        reply(thinking="", tools=(("toolu_1", "get_current_time", {}),)),
        reply(text="It's noon in Tokyo."),
        reply(text="You're welcome."),
    ]
    await Agent(rt).run(
        RunRequest(text="What time is it in Tokyo?"), recorder.emit, recorder.approve
    )
    assert "It's noon in Tokyo." in recorder.text()
    assert recorder.of("tool.end")[0]["ok"]

    assert fake.headers[0]["x-api-key"] == "sk-ant-x"  # The saved key reaches the SDK.
    second = fake.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert [b["type"] for b in second[1]["content"]] == ["thinking", "tool_use"]
    assert second[1]["content"][0]["signature"] == SIGNATURE
    assert second[2]["content"][0]["tool_use_id"] == "toolu_1"

    cid = recorder.of("conversation")[0]["conversation"]["id"]
    stored = rt.store.list_messages(cid)[1]
    assert stored["meta"]["native"]["content"][0]["signature"] == SIGNATURE
    assert "native" not in public_message(stored)["meta"]
    assert all("native" not in m["message"]["meta"] for m in recorder.of("message"))

    await Agent(rt).run(
        RunRequest(text="Thanks!", conversation_id=cid), recorder.emit, recorder.approve
    )
    third = fake.requests[2]["messages"]
    replayed = [b["type"] for m in third if m["role"] == "assistant" for b in m["content"]]
    assert "thinking" not in replayed  # Earlier turns go back without their thinking.
    assert "tool_use" in replayed


# Settings endpoints ---------------------------------------------------------------------------


def test_presets_and_testing_a_key_before_saving(make_runtime, fake):
    from fastapi.testclient import TestClient

    from bagley.server import create_app

    rt = make_runtime(env={"ANTHROPIC_API_KEY": "sk-env"})
    rt.update_preferences(
        {"machines": [{"id": "claude", "name": "Claude", "role": "cloud", "provider": "anthropic",
                       "base_url": "https://api.anthropic.com", "api_key": "sk-saved"}]}
    )  # fmt: skip
    with TestClient(create_app(rt), base_url="http://localhost") as client:
        presets = client.get("/api/machines/presets").json()
        claude = presets[0]
        assert (claude["id"], claude["provider"], claude["model"]) == (
            "claude",
            "anthropic",
            "claude-opus-5-5",
        )
        assert claude["key_in_env"] is True
        assert not next(p for p in presets if p["id"] == "openrouter")["key_in_env"]

        body = {
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-new",
        }
        res = client.post("/api/machines/test", json=body).json()
        assert res["ok"] and res["kind"] == "anthropic"
        assert [m["name"] for m in res["models"]] == ["claude-opus-5-5", "claude-haiku-5-5"]

        client.post("/api/machines/test", json={**body, "api_key": "", "id": "claude"})
        client.post("/api/machines/test", json={**body, "api_key": ""})
    keys = [h.get("x-api-key") for h in fake.model_headers]
    assert keys == ["sk-new", "sk-saved", "sk-env"]  # Typed, then saved, then the environment.


async def test_claude_is_the_default_model_when_none_is_chosen(make_runtime, fake):
    rt = make_runtime(
        env={"ANTHROPIC_API_KEY": "sk-env"},
        provider="anthropic",
        base_url="https://api.anthropic.com",
        model="",
    )
    prefs, _ = rt.preferences()
    assert await rt.resolve_model(await rt.provider(), prefs) == "claude-opus-5-5"
    rt.update_preferences(
        {"machines": [{"id": "claude", "name": "Claude", "role": "cloud", "provider": "anthropic",
                       "base_url": "https://api.anthropic.com"}], "routing": "claude"}
    )  # fmt: skip
    prefs, _ = rt.preferences()
    route = await rt.router.choose(prefs, "chat")
    assert (route.machine.id, route.model) == ("claude", "claude-opus-5-5")
    vision = await rt.router.choose(prefs, "vision")
    assert vision.model == "claude-opus-5-5"


async def test_no_key_anywhere_is_a_clear_error(monkeypatch, tmp_path):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # No `ant auth login` profile either.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    provider = ClaudeProvider("https://api.anthropic.com", "")
    with pytest.raises(LLMError, match="No Anthropic API key"):
        await provider.list_models()
    with pytest.raises(LLMError, match="No Anthropic API key"):
        await collect(provider, HISTORY, model="claude-opus-5-5")
    assert (await provider.capabilities("claude-opus-5-5")).tools
    await provider.aclose()

    monkeypatch.setenv("ANTHROPIC_PROFILE", "work")  # A named profile that doesn't exist.
    provider = ClaudeProvider("https://api.anthropic.com", "")
    with pytest.raises(LLMError, match="No Anthropic API key"):
        await provider.list_models()
    await provider.aclose()
