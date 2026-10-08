"""Model backends."""

from __future__ import annotations

from collections.abc import Mapping
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
    UnreachableError,
    Usage,
)
from bagley.llm.claude import ClaudeProvider
from bagley.llm.ollama import OllamaProvider
from bagley.llm.openai import OpenAIProvider
from bagley.llm.presets import PRESETS

__all__ = [
    "KEY_ENV",
    "ChatChunk",
    "ClaudeProvider",
    "LLMError",
    "Message",
    "ModelCapabilities",
    "ModelInfo",
    "OllamaProvider",
    "OpenAIProvider",
    "Provider",
    "ToolCall",
    "ToolsUnsupportedError",
    "UnreachableError",
    "Usage",
    "create_provider",
    "detect_kind",
    "key_from_env",
]

# Hosted APIs and the environment variables their keys usually live in.
KEY_ENV = {urlsplit(p["base_url"]).hostname: p["key_env"] for p in PRESETS.values()}


def key_from_env(kind: str, base_url: str, env: Mapping[str, str]) -> str:
    """The API key for a hosted API from the environment, when none is saved."""
    name = KEY_ENV.get(urlsplit(base_url).hostname or "")
    if name is None and kind == "anthropic":
        name = "ANTHROPIC_API_KEY"
    return env.get(name, "") if name else ""


async def detect_kind(base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Guess whether ``base_url`` is Ollama, the Anthropic API or a generic OpenAI-compatible one."""
    if urlsplit(base_url).hostname == "api.anthropic.com":
        return "anthropic"
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
    env: Mapping[str, str] | None = None,
) -> Provider:
    if kind == "auto":
        kind = await detect_kind(base_url, transport)
    if not api_key and env is not None:
        api_key = key_from_env(kind, base_url, env)
    if kind == "anthropic":
        return ClaudeProvider(base_url, api_key, transport=transport)
    cls = OllamaProvider if kind == "ollama" else OpenAIProvider
    return cls(base_url, api_key, transport=transport)
