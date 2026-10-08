"""Native Ollama backend (``/api/chat``). Supports context size, thinking and model pulls."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from bagley.llm.base import (
    ChatChunk,
    LLMError,
    Message,
    ModelCapabilities,
    ModelInfo,
    Provider,
    ToolCall,
    ToolsUnsupportedError,
    UnreachableError,
    Usage,
    new_call_id,
)


def _to_ollama(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        msg: dict[str, Any] = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            msg["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.arguments}}
                for c in map(ToolCall.from_message, m["tool_calls"])
            ]
        if m["role"] == "tool" and m.get("name"):
            msg["tool_name"] = m["name"]
        if m.get("images"):
            msg["images"] = m["images"]
        out.append(msg)
    return out


class OllamaProvider(Provider):
    kind = "ollama"
    supports_pull = True

    async def version(self) -> str:
        try:
            resp = await self._client.get("/api/version", timeout=5.0)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if resp.status_code != 200:
            raise LLMError(f"Unexpected response from Ollama ({resp.status_code}).")
        return str(resp.json().get("version", "unknown"))

    async def list_models(self) -> list[ModelInfo]:
        try:
            resp = await self._client.get("/api/tags", timeout=10.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        models = []
        for m in resp.json().get("models", []):
            details = m.get("details") or {}
            models.append(
                ModelInfo(
                    name=m.get("name") or m.get("model", ""),
                    size=m.get("size"),
                    parameter_size=details.get("parameter_size", ""),
                    family=details.get("family", ""),
                    quantization=details.get("quantization_level", ""),
                )
            )
        return sorted(models, key=lambda m: m.name)

    async def capabilities(self, model: str) -> ModelCapabilities:
        if model in self._caps:
            return self._caps[model]
        caps = ModelCapabilities()
        try:
            resp = await self._client.post("/api/show", json={"model": model}, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                listed = data.get("capabilities")
                if isinstance(listed, list):
                    caps.tools = "tools" in listed
                    caps.thinking = "thinking" in listed
                    caps.vision = "vision" in listed
                for key, value in (data.get("model_info") or {}).items():
                    if key.endswith(".context_length") and isinstance(value, int):
                        caps.context_length = value
                self._caps[model] = caps
        except httpx.HTTPError:
            pass
        return caps

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
        options: dict[str, Any] = {"temperature": temperature}
        if context_tokens:
            options["num_ctx"] = context_tokens
        if max_tokens:
            options["num_predict"] = max_tokens
        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_ollama(messages),
            "stream": True,
            "options": options,
        }
        if tools:
            payload["tools"] = tools
        if think is not None:
            payload["think"] = think
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    text = await self._error_text(resp)
                    if "think" in payload and "think" in text.lower():
                        retry = True  # Model rejects the reasoning switch; try again without it.
                    else:
                        raise self._http_error(resp.status_code, text, model)
                else:
                    retry = False
                if not retry:
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if data.get("error"):
                            raise self._http_error(500, str(data["error"]), model)
                        yield self._parse_chunk(data)
            if retry:
                async for chunk in self.chat(
                    messages,
                    model=model,
                    tools=tools,
                    temperature=temperature,
                    context_tokens=context_tokens,
                    think=None,
                    max_tokens=max_tokens,
                ):
                    yield chunk
        except httpx.TimeoutException as exc:
            raise UnreachableError(
                "The model server stopped responding.",
                hint="Large models can take a while to load. Try again, or pick a smaller model.",
            ) from exc
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc

    @staticmethod
    def _parse_chunk(data: dict[str, Any]) -> ChatChunk:
        msg = data.get("message") or {}
        chunk = ChatChunk(text=msg.get("content") or "", reasoning=msg.get("thinking") or "")
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            chunk.tool_calls.append(
                ToolCall(
                    id=call.get("id") or new_call_id(), name=fn.get("name", ""), arguments=args
                )
            )
        if data.get("done"):
            evals = data.get("eval_count")
            duration = data.get("eval_duration")
            tps = evals / (duration / 1e9) if evals and duration else None
            chunk.usage = Usage(
                prompt_tokens=data.get("prompt_eval_count"),
                completion_tokens=evals,
                tokens_per_second=tps,
            )
        return chunk

    def _http_error(self, status: int, text: str, model: str) -> LLMError:
        lower = text.lower()
        if "does not support tools" in lower:
            return ToolsUnsupportedError(text, status=status)
        if status == 404 or ("not found" in lower and "model" in lower):
            return LLMError(
                f"Model “{model}” is not installed.",
                hint=f"Run `ollama pull {model}` or choose another model.",
                status=status,
            )
        return LLMError(f"Ollama error: {text}", status=status)

    async def pull(self, model: str) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream(
                "POST", "/api/pull", json={"model": model, "stream": True}, timeout=None
            ) as resp:
                if resp.status_code >= 400:
                    raise LLMError(f"Pull failed: {await self._error_text(resp)}")
                async for line in resp.aiter_lines():
                    if line.strip():
                        data = json.loads(line)
                        if data.get("error"):
                            raise LLMError(f"Pull failed: {data['error']}")
                        yield data
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        try:
            resp = await self._client.post(
                "/api/embed", json={"model": model, "input": texts}, timeout=120.0
            )
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if resp.status_code >= 400:
            raise self._http_error(resp.status_code, await self._error_text(resp), model)
        return resp.json().get("embeddings", [])

    async def loaded(self) -> list[dict[str, Any]]:
        try:
            resp = await self._client.get("/api/ps", timeout=10.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        return [
            {
                "name": m.get("name") or m.get("model", ""),
                "size": m.get("size"),
                "size_vram": m.get("size_vram"),
                "expires_at": m.get("expires_at"),
                "context_length": m.get("context_length"),
            }
            for m in resp.json().get("models", [])
        ]

    async def unload(self, model: str) -> None:
        try:
            resp = await self._client.post(
                "/api/generate", json={"model": model, "keep_alive": 0}, timeout=30.0
            )
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if resp.status_code >= 400:
            raise self._http_error(resp.status_code, await self._error_text(resp), model)

    async def delete(self, model: str) -> None:
        try:
            resp = await self._client.request(
                "DELETE", "/api/delete", json={"model": model}, timeout=30.0
            )
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if resp.status_code >= 400:
            raise self._http_error(resp.status_code, await self._error_text(resp), model)
        self._caps.pop(model, None)
