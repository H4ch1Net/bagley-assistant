"""Anthropic backend: Claude through the official ``anthropic`` SDK and the Messages API.

Bagley keeps history in the OpenAI shape (system, user, assistant and tool messages, function
tool calls, base64 images). This module turns it into Messages API content blocks and streams
the reply back as ``ChatChunk``s.

* Thinking. Models that always think (Claude Opus 5.5, Sonnet 5.5, Fable) get adaptive
  thinking; where the API offers it, their notes between tool calls come back as reasoning
  (``display: "updates"``) so a long tool loop doesn't go quiet. ``think`` asks for summarized
  reasoning on every model with adaptive thinking.
* Thinking blocks of the current turn travel in ``ChatChunk.native`` and are replayed unchanged.
  ``drop_block`` makes the API drop a block whose conversation changed (the system prompt carries
  the time, tool routing changes the tool set) instead of rejecting the request.
* Refusals. On the Claude API, models that support it retry a policy decline on another model
  server-side (``fallbacks: "default"``).
* Prompt caching is automatic (top-level ``cache_control``).
* Anything the API rejects in that setup is retried once without the extras.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from bagley.llm.base import (
    ChatChunk,
    LLMError,
    Message,
    ModelCapabilities,
    ModelInfo,
    Provider,
    ToolCall,
    UnreachableError,
    Usage,
)

try:
    import anthropic
except ImportError:  # pragma: no cover - a core dependency, but keep the error readable.
    anthropic = None  # type: ignore[assignment]

DEFAULT_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 32_000  # Every current Claude model allows at least this much output.
HISTORY_TOKENS = 100_000  # History budget per request; the models take far more.

BINDING_BETA = "thinking-binding-controls-2026-08-01"
UPDATES_BETA = "thinking-display-updates-2026-08-18"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Model id prefixes. A prefix matches the id itself and ids that continue it with "-".
ALWAYS_THINKS = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5-5",
)
ADAPTIVE = (
    *ALWAYS_THINKS,
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
)
UPDATES = (
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-mythos-5-1",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
)
FALLBACKS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")

REPLAYED = {"text", "thinking", "redacted_thinking", "tool_use"}
THINKING = {"thinking", "redacted_thinking"}
_IMAGE_TYPES = {
    "iVBOR": "image/png",
    "/9j/": "image/jpeg",
    "R0lG": "image/gif",
    "UklG": "image/webp",
}


def matches(model: str, prefixes: tuple[str, ...]) -> bool:
    return any(model == p or model.startswith(p + "-") for p in prefixes)


def _image(data: str) -> dict[str, Any]:
    mime = next((t for p, t in _IMAGE_TYPES.items() if data.startswith(p)), "image/png")
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}


def to_tool(spec: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI function spec as a Messages API tool."""
    fn = spec.get("function", spec)
    schema = fn.get("parameters") or {"type": "object", "properties": {}}
    return {"name": fn["name"], "description": fn.get("description", ""), "input_schema": schema}


def _replay(native: Any, keep_thinking: bool) -> list[dict[str, Any]] | None:
    if not isinstance(native, dict) or native.get("kind") != "anthropic":
        return None
    blocks = [b for b in native.get("content") or [] if isinstance(b, dict)]
    return [
        b
        for b in blocks
        if b.get("type") in REPLAYED
        and (keep_thinking or b["type"] not in THINKING)
        and not (b["type"] == "text" and not b.get("text"))
    ]


def to_wire(messages: list[Message], *, keep_thinking: bool = True) -> tuple[str, list[Message]]:
    """The system prompt and Messages API messages for Bagley's history."""
    system: list[str] = []
    out: list[Message] = []

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:  # The API merges them anyway; tool results must.
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role, text = m.get("role"), m.get("content") or ""
        if role == "system":
            if out:  # Later system notes go in as text; not every model takes system turns.
                add("user", [{"type": "text", "text": f"<system-note>\n{text}\n</system-note>"}])
            elif text:
                system.append(text)
        elif role == "user":
            blocks = [_image(i) for i in m.get("images") or []]
            if text:
                blocks.append({"type": "text", "text": text})
            add("user", blocks)
        elif role == "assistant":
            blocks = _replay(m.get("native"), keep_thinking)
            if blocks is None:
                blocks = [{"type": "text", "text": text}] if text else []
                for call in m.get("tool_calls") or []:
                    tc = ToolCall.from_message(call)
                    blocks.append(
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                    )
            add("assistant", blocks)
        elif role == "tool":
            body: Any = text or "(no output)"
            if m.get("images"):
                body = [{"type": "text", "text": body}, *(_image(i) for i in m["images"])]
            add(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id") or "",
                        "content": body,
                    }
                ],
            )
    return "\n\n".join(system), out


def after_fallback(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Content to keep and replay. Before a fallback boundary only the text counts: the model
    that declined left thinking and tool calls the next one never made."""
    last = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    kept = [b for b in content[:last] if b.get("type") == "text"] if last >= 0 else []
    return [*kept, *(b for b in content[last + 1 :] if b.get("type") in REPLAYED)]


class ClaudeProvider(Provider):
    kind = "anthropic"
    history_tokens = HISTORY_TOKENS

    def __init__(
        self, base_url: str = "", api_key: str = "", *, transport: Any = None, client: Any = None
    ) -> None:
        # No httpx client of our own: the SDK has one. ``transport`` is accepted for symmetry.
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.display_url = self.base_url
        self.api_key = api_key
        self._caps: dict[str, ModelCapabilities] = {}
        self._sdk_client = client

    async def aclose(self) -> None:
        if self._sdk_client is not None:
            await self._sdk_client.close()

    @property
    def is_claude_api(self) -> bool:
        return urlsplit(self.base_url).hostname == "api.anthropic.com"

    def _sdk(self) -> Any:
        if self._sdk_client is None:
            if anthropic is None:
                raise LLMError(
                    "Claude support isn't installed.",
                    hint="Install it with: pip install 'bagley[claude]' (the anthropic package).",
                )
            kwargs: dict[str, Any] = {"max_retries": 1, "timeout": 600.0}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url != DEFAULT_URL:
                kwargs["base_url"] = self.base_url
            try:
                self._sdk_client = anthropic.AsyncAnthropic(**kwargs)
            except anthropic.AnthropicError as exc:
                raise LLMError(
                    "No Anthropic API key.",
                    hint="Add the key under Settings → Model & machines, or set ANTHROPIC_API_KEY "
                    "where Bagley runs.",
                ) from exc
        return self._sdk_client

    # Models -----------------------------------------------------------------------------------

    async def version(self) -> str:
        await self.list_models()
        return "anthropic"

    async def list_models(self) -> list[ModelInfo]:
        client = self._sdk()
        try:
            models = [m async for m in client.models.list(limit=100)]
        except anthropic.APIError as exc:
            raise self._error(exc, "") from exc
        return [
            ModelInfo(name=m.id, family=getattr(m, "display_name", "") or "claude") for m in models
        ]

    async def capabilities(self, model: str) -> ModelCapabilities:
        if model not in self._caps:
            context = None
            client = self._sdk()
            try:
                info = await client.models.retrieve(model)
                context = getattr(info, "max_input_tokens", None)
            except anthropic.APIError:
                pass  # Unknown here; the request itself will say what's wrong.
            self._caps[model] = ModelCapabilities(
                tools=True, thinking=matches(model, ADAPTIVE), vision=True, context_length=context
            )
        return self._caps[model]

    # Chat -------------------------------------------------------------------------------------

    def _thinking(self, model: str, think: bool | None) -> dict[str, Any] | None:
        if not matches(model, ADAPTIVE):
            return None
        if think:
            config: dict[str, Any] = {"type": "adaptive", "display": "summarized"}
        elif matches(model, ALWAYS_THINKS):
            config = {"type": "adaptive"}  # Same as leaving it out on these models.
            if matches(model, UPDATES):
                config["display"] = "updates"
        else:
            return None
        config["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
        return config

    def request(
        self,
        messages: list[Message],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        think: bool | None = None,
        max_tokens: int | None = None,
        *,
        plain: bool = False,
    ) -> dict[str, Any]:
        """Keyword arguments for ``client.beta.messages.create``. ``plain`` leaves out thinking
        settings, replayed thinking and the betas, for a retry after the API rejected them."""
        system, wire = to_wire(messages, keep_thinking=not plain)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or MAX_TOKENS,
            "messages": wire,
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = [to_tool(t) for t in tools]
        if plain:
            return params
        betas: list[str] = []
        thinking = self._thinking(model, think)
        if thinking:
            params["thinking"] = thinking
            betas.append(BINDING_BETA)
            if thinking.get("display") == "updates":
                betas.append(UPDATES_BETA)
        if self.is_claude_api and matches(model, FALLBACKS):
            params["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        if betas:
            params["betas"] = betas
        return params

    async def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,  # Not sent: current Claude models reject sampling settings.
        context_tokens: int | None = None,
        think: bool | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ChatChunk]:
        client = self._sdk()
        params = self.request(messages, model, tools, think, max_tokens)
        plain = self.request(messages, model, tools, think, max_tokens, plain=True)
        while True:
            started = False
            try:
                async for chunk in self._stream(client, params, model):
                    started = True
                    yield chunk
                return
            except anthropic.BadRequestError as exc:
                if started or params == plain:
                    raise self._error(exc, model) from exc
                params = plain  # Decided before any output, so a retry is safe.
            except anthropic.APIError as exc:
                raise self._error(exc, model) from exc

    async def _stream(
        self, client: Any, params: dict[str, Any], model: str
    ) -> AsyncIterator[ChatChunk]:
        blocks: dict[int, dict[str, Any]] = {}
        inputs: dict[int, str] = {}
        prompt_tokens = output_tokens = None
        stop_reason = None
        stop_details = None
        first = None
        stream = await client.beta.messages.create(**params, stream=True)
        async for event in stream:
            kind = event.type
            if kind == "message_start":
                u = event.message.usage
                prompt_tokens = sum(
                    getattr(u, f, 0) or 0
                    for f in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
            elif kind == "content_block_start":
                block = event.content_block.to_dict(mode="json")
                blocks[event.index] = block
                if block.get("type") == "tool_use":
                    inputs[event.index] = ""
            elif kind == "content_block_delta":
                block = blocks.setdefault(event.index, {"type": "text", "text": ""})
                delta = event.delta
                if delta.type == "text_delta":
                    block["text"] = (block.get("text") or "") + delta.text
                    first = first or time.monotonic()
                    yield ChatChunk(text=delta.text)
                elif delta.type == "thinking_delta":
                    block["thinking"] = (block.get("thinking") or "") + delta.thinking
                    if delta.thinking:
                        first = first or time.monotonic()
                        yield ChatChunk(reasoning=delta.thinking)
                elif delta.type == "signature_delta":
                    block["signature"] = (block.get("signature") or "") + delta.signature
                elif delta.type == "input_json_delta":
                    inputs[event.index] = inputs.get(event.index, "") + delta.partial_json
                elif delta.type == "citations_delta":
                    cites = block.get("citations") or []
                    block["citations"] = [*cites, delta.citation.to_dict(mode="json")]
            elif kind == "content_block_stop":
                raw = inputs.pop(event.index, None)
                if raw is not None:
                    try:
                        blocks[event.index]["input"] = json.loads(raw) if raw.strip() else {}
                    except json.JSONDecodeError:
                        blocks[event.index]["input"] = {"_raw": raw}
            elif kind == "message_delta":
                stop_reason = event.delta.stop_reason or stop_reason
                stop_details = event.delta.stop_details or stop_details
                output_tokens = event.usage.output_tokens

        content = after_fallback([blocks[i] for i in sorted(blocks)])
        calls = [
            ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
            for b in content
            if b.get("type") == "tool_use"
        ]
        if stop_reason == "refusal":
            category = getattr(stop_details, "category", None)
            raise LLMError(
                f"Claude declined this request{f' ({category})' if category else ''}.",
                hint="Rephrase it, or pick another model or machine for this chat.",
            )
        if stop_reason in ("max_tokens", "model_context_window_exceeded") and calls:
            raise LLMError(
                "Claude's reply was cut off before its tool call was complete.",
                hint="Ask for a smaller step, or start a new chat if this one is very long.",
            )
        tps = None
        if output_tokens and first:
            tps = output_tokens / max(time.monotonic() - first, 0.001)
        yield ChatChunk(
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=prompt_tokens, completion_tokens=output_tokens, tokens_per_second=tps
            ),
            native={"kind": "anthropic", "model": model, "content": content},
        )

    # Errors -----------------------------------------------------------------------------------

    def _error(self, exc: Exception, model: str) -> LLMError:
        status = getattr(exc, "status_code", None)
        message = str(getattr(exc, "message", "") or exc)[:500]
        if isinstance(exc, anthropic.AuthenticationError):
            return LLMError(
                "Anthropic rejected the API key.",
                hint="Check the key under Settings → Model & machines, or ANTHROPIC_API_KEY.",
                status=401,
            )
        if isinstance(exc, anthropic.PermissionDeniedError):
            return LLMError(
                f"This Anthropic key can't use {model or 'that'}: {message}", status=403
            )
        if isinstance(exc, anthropic.NotFoundError):
            return LLMError(
                f"Model “{model}” was not found on the Anthropic API.",
                hint="Pick one from the list under Settings → Model & machines.",
                status=404,
            )
        if isinstance(exc, anthropic.RateLimitError):
            retry = exc.response.headers.get("retry-after", "")
            return UnreachableError(
                "Anthropic is rate-limiting this key.",
                hint=f"Try again in {retry}s." if retry else "Try again shortly.",
                status=429,
            )
        if isinstance(exc, anthropic.APIConnectionError):  # Includes timeouts.
            return UnreachableError(
                f"Can't reach the Anthropic API at {self.display_url}.",
                hint="Check the internet connection, or the URL if you use a proxy.",
            )
        if status and status >= 500:
            return UnreachableError(f"The Anthropic API is unavailable ({status}).", status=status)
        return LLMError(f"Anthropic API error ({status or '?'}): {message}", status=status)
