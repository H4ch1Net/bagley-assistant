"""Model backends."""

from __future__ import annotations

from urllib.parse import urlsplit

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
    Usage,
)
from bagley.llm.ollama import OllamaProvider
from bagley.llm.openai import OpenAIProvider

__all__ = [
    "ChatChunk",
    "LLMError",
    "Message",
    "ModelCapabilities",
    "ModelInfo",
    "OllamaProvider",
    "OpenAIProvider",
    "Provider",
    "ToolCall",
    "ToolsUnsupportedError",
    "Usage",
    "create_provider",
    "detect_kind",
]


async def detect_kind(base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Guess whether ``base_url`` is an Ollama server or a generic OpenAI-compatible one."""
    if urlsplit(base_url).path.rstrip("/").endswith("/v1"):
        return "openai"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=3.0) as client:
            resp = await client.get(base_url.rstrip("/") + "/api/version")
            if resp.status_code == 200 and "version" in resp.json():
                return "ollama"
    except (httpx.HTTPError, ValueError):
        # Unreachable: assume Ollama on its default port, otherwise OpenAI-compatible.
        return "ollama" if urlsplit(base_url).port == 11434 else "openai"
    return "openai"


async def create_provider(
    kind: str,
    base_url: str,
    api_key: str = "",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Provider:
    if kind == "auto":
        kind = await detect_kind(base_url, transport)
    cls = OllamaProvider if kind == "ollama" else OpenAIProvider
    return cls(base_url, api_key, transport=transport)
