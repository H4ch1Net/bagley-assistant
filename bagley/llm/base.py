"""Provider-neutral types shared by the model backends."""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

Message = dict[str, Any]


def new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_message(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
        }

    @classmethod
    def from_message(cls, data: dict[str, Any]) -> ToolCall:
        fn = data.get("function", {})
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        return cls(id=data.get("id") or new_call_id(), name=fn.get("name", ""), arguments=args)


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tokens_per_second: float | None = None


@dataclass
class ChatChunk:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None


@dataclass
class ModelInfo:
    name: str
    size: int | None = None
    parameter_size: str = ""
    family: str = ""
    quantization: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ModelCapabilities:
    tools: bool | None = None  # None means unknown: try native tools, fall back on error
    thinking: bool = False
    vision: bool = False
    context_length: int | None = None


class LLMError(Exception):
    """A model backend failure with a user-facing hint."""

    def __init__(self, message: str, hint: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status = status


class ToolsUnsupportedError(LLMError):
    pass


class Provider(ABC):
    kind = "base"
    supports_pull = False

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0),
        )
        self._caps: dict[str, ModelCapabilities] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @abstractmethod
    async def version(self) -> str: ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        context_tokens: int | None = None,
        think: bool | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ChatChunk]: ...

    async def complete(self, messages: list[Message], *, model: str, **kwargs: Any) -> str:
        parts = [chunk.text async for chunk in self.chat(messages, model=model, **kwargs)]
        return "".join(parts)

    def pull(self, model: str) -> AsyncIterator[dict[str, Any]]:
        raise LLMError("This server cannot download models.")

    # Error helpers --------------------------------------------------------------------------

    def _unreachable(self, exc: Exception) -> LLMError:
        return LLMError(
            f"Can't reach the model server at {self.base_url}.",
            hint="Start your model server (for Ollama: `ollama serve`) or change the server "
            "URL in Settings → Model.",
        )

    @staticmethod
    async def _error_text(resp: httpx.Response) -> str:
        body = (await resp.aread()).decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return body.strip()[:500] or resp.reason_phrase
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            err = err.get("message") or json.dumps(err)
        return str(err)[:500]
