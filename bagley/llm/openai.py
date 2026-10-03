"""OpenAI-compatible backend: LM Studio, llama.cpp server, vLLM, LocalAI, Jan, OpenRouter, OpenAI."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from bagley.llm.base import (
    ChatChunk,
    LLMError,
    Message,
    ModelInfo,
    Provider,
    ToolCall,
    ToolsUnsupportedError,
    Usage,
    new_call_id,
)

_ALLOWED_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}


def openai_base(url: str) -> str:
    """``http://host:1234`` becomes ``http://host:1234/v1``; explicit paths are kept."""
    url = url.rstrip("/")
    return url + "/v1" if urlsplit(url).path in ("", "/") else url


class OpenAIProvider(Provider):
    kind = "openai"

    def __init__(self, base_url: str, api_key: str = "", **kwargs: Any) -> None:
        super().__init__(openai_base(base_url), api_key, **kwargs)
        self.display_url = base_url.rstrip("/")

    async def version(self) -> str:
        await self.list_models()
        return "openai-compatible"

    async def list_models(self) -> list[ModelInfo]:
        try:
            resp = await self._client.get("/models", timeout=10.0)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if resp.status_code == 401:
            raise LLMError(
                "The model server rejected the API key.",
                hint="Set the API key in Settings → Model.",
                status=401,
            )
        if resp.status_code >= 400:
            raise LLMError(
                f"Couldn't list models ({resp.status_code}).",
                hint="Check that the server URL points at an OpenAI-compatible API.",
                status=resp.status_code,
            )
        data = resp.json().get("data", [])
        return sorted((ModelInfo(name=m["id"]) for m in data if m.get("id")), key=lambda m: m.name)

    async def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        context_tokens: int | None = None,
        think: bool | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ChatChunk]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{k: v for k, v in m.items() if k in _ALLOWED_KEYS} for m in messages],
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        if max_tokens:
            payload["max_tokens"] = max_tokens
        pending: dict[int, dict[str, Any]] = {}
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    raise self._http_error(resp.status_code, await self._error_text(resp), model)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    data = json.loads(body)
                    if data.get("error"):
                        err = data["error"]
                        raise LLMError(
                            str(err.get("message", err) if isinstance(err, dict) else err)
                        )
                    chunk = ChatChunk()
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        chunk.text += delta.get("content") or ""
                        chunk.reasoning += (
                            delta.get("reasoning_content") or delta.get("reasoning") or ""
                        )
                        for part in delta.get("tool_calls") or []:
                            slot = pending.setdefault(
                                part.get("index", len(pending)), {"id": "", "name": "", "args": ""}
                            )
                            fn = part.get("function") or {}
                            slot["id"] = part.get("id") or slot["id"]
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
                    if data.get("usage"):
                        u = data["usage"]
                        chunk.usage = Usage(
                            prompt_tokens=u.get("prompt_tokens"),
                            completion_tokens=u.get("completion_tokens"),
                        )
                    if chunk.text or chunk.reasoning or chunk.usage:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise LLMError("The model server stopped responding.") from exc
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if pending:
            calls = []
            for slot in (pending[i] for i in sorted(pending)):
                call = ToolCall.from_message(
                    {
                        "id": slot["id"],
                        "function": {"name": slot["name"], "arguments": slot["args"]},
                    }
                )
                call.id = call.id or new_call_id()
                calls.append(call)
            yield ChatChunk(tool_calls=calls)

    @staticmethod
    def _http_error(status: int, text: str, model: str) -> LLMError:
        lower = text.lower()
        markers = (
            "support",
            "jinja",
            "not allowed",
            "tool choice",
            "tool_choice",
            "tool-call-parser",
        )
        if "tool" in lower and any(w in lower for w in markers):
            return ToolsUnsupportedError(text, status=status)
        if status == 404 or "model_not_found" in lower or "no such model" in lower:
            return LLMError(
                f"Model “{model}” was not found on the server.",
                hint="Pick an available model in Settings → Model.",
                status=status,
            )
        if status == 401:
            return LLMError("The model server rejected the API key.", status=401)
        return LLMError(f"Model server error ({status}): {text}", status=status)

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        try:
            resp = await self._client.post(
                "/embeddings", json={"model": model, "input": texts}, timeout=120.0
            )
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if resp.status_code >= 400:
            raise self._http_error(resp.status_code, await self._error_text(resp), model)
        data = sorted(resp.json().get("data", []), key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]
