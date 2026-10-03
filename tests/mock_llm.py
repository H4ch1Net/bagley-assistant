"""A scripted stand-in for Ollama and OpenAI-compatible servers.

Used by the test suite (through ``httpx.ASGITransport``) and by ``scripts/screenshots.py``
(as a real HTTP server). It speaks the actual wire formats so the provider code is exercised
end to end.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


@dataclass
class Reply:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: tuple[int, str] | None = None
    text_tool_call: bool = False  # Emit the tool call as <tool_call> text instead of natively.


MODELS = {
    "qwen3:8b": {
        "capabilities": ["completion", "tools", "thinking"],
        "size": 5_225_000_000,
        "params": "8.2B",
        "family": "qwen3",
    },
    "llama3.2:3b": {
        "capabilities": ["completion", "tools"],
        "size": 2_019_000_000,
        "params": "3.2B",
        "family": "llama",
    },
    "gemma2:2b": {
        "capabilities": ["completion"],
        "size": 1_629_000_000,
        "params": "2.6B",
        "family": "gemma2",
    },
}


def _chunks(text: str, size: int = 6) -> list[str]:
    parts = re.findall(r"\S+\s*|\s+", text)
    out, buf = [], ""
    for p in parts:
        buf += p
        if len(buf) >= size:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


class MockLLM:
    def __init__(self, *, delay: float = 0.0, models: dict[str, Any] | None = None) -> None:
        self.delay = delay
        self.models = dict(MODELS if models is None else models)
        self.script: list[Reply] = []
        self.requests: list[dict[str, Any]] = []
        self.app = self._build()

    # Behaviour ------------------------------------------------------------------------------

    def next_reply(self, payload: dict[str, Any]) -> Reply:
        messages = payload.get("messages", [])
        last = messages[-1] if messages else {"role": "user", "content": ""}
        if "Write a title" in (last.get("content") or ""):
            return Reply(text=self._title(last["content"]))
        if self.script:
            return self.script.pop(0)
        prompt_tools = bool(messages) and "<tool_call>" in (messages[0].get("content") or "")
        return demo_brain(messages, has_tools=bool(payload.get("tools")) or prompt_tools)

    @staticmethod
    def _title(prompt: str) -> str:
        msg = prompt.split("Message:", 1)[-1].lower()
        for key, title in (
            ("weather", "Weekend weather in Lisbon"),
            ("remember", "Personal preferences"),
            ("tip", "Splitting the dinner bill"),
            ("note", "Trip notes"),
        ):
            if key in msg:
                return title
        return "Quick question"

    # Wire formats ---------------------------------------------------------------------------

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/api/version")
        async def version() -> dict[str, str]:
            return {"version": "0.12.0-mock"}

        @app.get("/api/tags")
        async def tags() -> dict[str, Any]:
            return {
                "models": [
                    {
                        "name": name,
                        "model": name,
                        "size": spec["size"],
                        "details": {
                            "family": spec["family"],
                            "parameter_size": spec["params"],
                            "quantization_level": "Q4_K_M",
                        },
                    }
                    for name, spec in self.models.items()
                ]
            }

        @app.post("/api/show")
        async def show(request: Request) -> JSONResponse:
            name = (await request.json()).get("model")
            if name not in self.models:
                return JSONResponse({"error": f"model '{name}' not found"}, status_code=404)
            return JSONResponse(
                {
                    "capabilities": self.models[name]["capabilities"],
                    "model_info": {"general.architecture": "x", "x.context_length": 40960},
                }
            )

        @app.post("/api/pull")
        async def pull(request: Request) -> StreamingResponse:
            name = (await request.json()).get("model")

            async def stream():
                yield json.dumps({"status": "pulling manifest"}) + "\n"
                total = 1000
                for done in range(0, total + 1, 250):
                    await asyncio.sleep(self.delay)
                    yield (
                        json.dumps(
                            {
                                "status": "pulling abc123",
                                "digest": "sha256:abc123",
                                "total": total,
                                "completed": done,
                            }
                        )
                        + "\n"
                    )
                self.models[name] = {
                    "capabilities": ["completion", "tools"],
                    "size": total,
                    "params": "1B",
                    "family": "mock",
                }
                yield json.dumps({"status": "success"}) + "\n"

            return StreamingResponse(stream(), media_type="application/x-ndjson")

        @app.post("/api/chat")
        async def ollama_chat(request: Request):
            payload = await request.json()
            self.requests.append(payload)
            model = payload.get("model")
            if model not in self.models:
                return JSONResponse(
                    {"error": f"model '{model}' not found, try pulling it first"}, status_code=404
                )
            if payload.get("tools") and "tools" not in self.models[model]["capabilities"]:
                return JSONResponse(
                    {"error": f"registry.ollama.ai/library/{model} does not support tools"},
                    status_code=400,
                )
            reply = self.next_reply(payload)
            if reply.error:
                return JSONResponse({"error": reply.error[1]}, status_code=reply.error[0])

            async def stream():
                for part in _chunks(reply.reasoning):
                    await asyncio.sleep(self.delay)
                    yield (
                        json.dumps(
                            {
                                "message": {"role": "assistant", "content": "", "thinking": part},
                                "done": False,
                            }
                        )
                        + "\n"
                    )
                text = reply.text
                if reply.tool_calls and (reply.text_tool_call or not payload.get("tools")):
                    text += "".join(
                        f'<tool_call>{{"name": "{n}", "arguments": {json.dumps(a)}}}</tool_call>'
                        for n, a in reply.tool_calls
                    )
                for part in _chunks(text):
                    await asyncio.sleep(self.delay)
                    yield (
                        json.dumps(
                            {"message": {"role": "assistant", "content": part}, "done": False}
                        )
                        + "\n"
                    )
                if reply.tool_calls and payload.get("tools") and not reply.text_tool_call:
                    calls = [{"function": {"name": n, "arguments": a}} for n, a in reply.tool_calls]
                    yield (
                        json.dumps(
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": calls,
                                },
                                "done": False,
                            }
                        )
                        + "\n"
                    )
                tokens = max(1, len(text) // 4)
                yield (
                    json.dumps(
                        {
                            "message": {"role": "assistant", "content": ""},
                            "done": True,
                            "done_reason": "stop",
                            "prompt_eval_count": 812,
                            "eval_count": tokens,
                            "eval_duration": int(tokens / 42.0 * 1e9),
                        }
                    )
                    + "\n"
                )

            return StreamingResponse(stream(), media_type="application/x-ndjson")

        @app.get("/v1/models")
        async def openai_models() -> dict[str, Any]:
            return {"object": "list", "data": [{"id": n, "object": "model"} for n in self.models]}

        @app.post("/v1/chat/completions")
        async def openai_chat(request: Request):
            payload = await request.json()
            self.requests.append(payload)
            if payload.get("tools") and "tools" not in self.models.get(
                payload.get("model"), {}
            ).get("capabilities", []):
                return JSONResponse(
                    {"error": {"message": "This model does not support tools"}}, status_code=400
                )
            reply = self.next_reply(payload)
            if reply.error:
                return JSONResponse(
                    {"error": {"message": reply.error[1]}}, status_code=reply.error[0]
                )

            def sse(delta: dict[str, Any], finish: str | None = None) -> str:
                return (
                    "data: "
                    + json.dumps(
                        {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    )
                    + "\n\n"
                )

            async def stream():
                for part in _chunks(reply.reasoning):
                    yield sse({"reasoning_content": part})
                for part in _chunks(reply.text):
                    await asyncio.sleep(self.delay)
                    yield sse({"content": part})
                for i, (name, args) in enumerate(reply.tool_calls):
                    raw = json.dumps(args)
                    half = len(raw) // 2
                    yield sse(
                        {
                            "tool_calls": [
                                {
                                    "index": i,
                                    "id": f"call_{i}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": raw[:half]},
                                }
                            ]
                        }
                    )
                    yield sse({"tool_calls": [{"index": i, "function": {"arguments": raw[half:]}}]})
                yield sse({}, "tool_calls" if reply.tool_calls else "stop")
                yield (
                    "data: "
                    + json.dumps(
                        {"choices": [], "usage": {"prompt_tokens": 700, "completion_tokens": 20}}
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream")

        return app


# Demo conversation used for screenshots -------------------------------------------------------


def demo_brain(messages: list[dict[str, Any]], has_tools: bool) -> Reply:
    last = messages[-1]
    user = next(
        (
            m["content"]
            for m in reversed(messages)
            if m["role"] == "user" and "<tool_response" not in m["content"]
        ),
        "",
    )
    lower = user.lower()
    if last["role"] == "tool" or "<tool_response" in (last.get("content") or ""):
        result = last.get("content") or ""
        name = last.get("name") or last.get("tool_name") or ""
        if name == "get_weather" or "forecast" in result:
            return Reply(
                text=(
                    "Lisbon is being smug about it: **22°C and mostly clear** right now, light breeze from the west.\n\n"
                    "| Day | Conditions | High / Low | Rain |\n|---|---|---|---|\n"
                    "| Sat | Mainly clear | 23° / 16° | 5% |\n| Sun | Partly cloudy | 21° / 15° | 20% |\n"
                    "| Mon | Light showers | 19° / 14° | 60% |\n\n"
                    "Saturday is your day for the coast. Pack a light jacket for Monday."
                )
            )
        if name == "calculate" or " = " in result:
            return Reply(
                text="That comes to **€41.18 each** (€144.12 including the 12% tip, split 4 ways). Someone still has to argue about who had the dessert."
            )
        if name == "remember" or "Saved memory" in result:
            return Reply(
                text="Noted. I'll stick to metric units and keep code examples in Python from now on."
            )
        if name == "write_file" or "Created" in result:
            return Reply(
                text="Done. `trip/lisbon.md` now has the forecast and a packing list. Shall I add restaurant ideas as well?"
            )
        return Reply(text="Here is what I found.")
    if has_tools and "weather" in lower:
        return Reply(
            reasoning="The user wants the weekend forecast for Lisbon. I should call get_weather rather than guess.",
            tool_calls=[("get_weather", {"location": "Lisbon, Portugal", "days": 3})],
        )
    if has_tools and "tip" in lower:
        return Reply(tool_calls=[("calculate", {"expression": "128.68 * 1.12 / 4"})])
    if has_tools and "remember" in lower:
        return Reply(
            tool_calls=[("remember", {"fact": "Prefers metric units and Python for code examples"})]
        )
    if has_tools and "note" in lower:
        return Reply(
            tool_calls=[
                (
                    "write_file",
                    {
                        "path": "trip/lisbon.md",
                        "content": "# Lisbon\n\n- Sat: clear, 23°C\n- Pack: light jacket\n",
                    },
                )
            ]
        )
    return Reply(
        text=(
            "Good evening. I run entirely on this machine, so nothing you type leaves it unless you ask me to look something up.\n\n"
            "I can search the web, read pages, check the weather, do exact maths, manage files in your workspace and remember your preferences."
        )
    )


# Fake web services for tool calls ---------------------------------------------------------------

GEOCODE = {
    "results": [
        {
            "name": "Lisbon",
            "latitude": 38.72,
            "longitude": -9.13,
            "country": "Portugal",
            "country_code": "PT",
            "admin1": "Lisbon",
        }
    ]
}
FORECAST = {
    "current": {
        "time": "2026-10-03T18:00",
        "temperature_2m": 22.1,
        "apparent_temperature": 21.4,
        "relative_humidity_2m": 58,
        "weather_code": 1,
        "wind_speed_10m": 14.2,
        "precipitation": 0.0,
    },
    "daily": {
        "time": ["2026-10-03", "2026-10-04", "2026-10-05"],
        "weather_code": [1, 2, 80],
        "temperature_2m_max": [23.0, 21.2, 19.4],
        "temperature_2m_min": [16.1, 15.0, 14.2],
        "precipitation_probability_max": [5, 20, 60],
    },
}
DDG_HTML = """
<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=x">First <b>result</b></a>
<a class="result__snippet" href="#">Snippet &amp; one</a></div>
<div class="result"><a rel="nofollow" class="result__a" href="https://example.org/two">Second result</a>
<a class="result__snippet" href="#">Snippet two</a></div>
"""


WEB_REQUESTS: list[httpx.Request] = []


def fake_web(request: httpx.Request) -> httpx.Response:
    WEB_REQUESTS.append(request)
    host = request.url.host
    if request.url.path == "/to-private":
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/preferences"})
    if request.url.path == "/huge":
        return httpx.Response(200, content=b"x" * 3_100_000, headers={"content-type": "text/plain"})
    if host == "geocoding-api.open-meteo.com":
        return httpx.Response(200, json=GEOCODE)
    if host == "api.open-meteo.com":
        return httpx.Response(200, json=FORECAST)
    if host == "html.duckduckgo.com":
        return httpx.Response(200, text=DDG_HTML)
    return httpx.Response(
        200,
        text="<html><head><title>Example</title></head><body><p>Hello page</p></body></html>",
        headers={"content-type": "text/html"},
    )


def fake_web_transport() -> httpx.MockTransport:
    return httpx.MockTransport(fake_web)
