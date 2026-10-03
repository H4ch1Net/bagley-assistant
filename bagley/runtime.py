"""Shared application state: config, storage, tools and the active model provider."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from pydantic import ValidationError

from bagley.automations import Scheduler
from bagley.config import Preferences, ServerConfig, resolve_preferences
from bagley.llm import LLMError, Provider, create_provider
from bagley.mcp import McpManager
from bagley.store import Store
from bagley.tools import ToolContext, build_registry

log = logging.getLogger("bagley")

_PROVIDER_KEYS = {"provider", "base_url", "api_key"}


class Runtime:
    def __init__(
        self,
        config: ServerConfig,
        *,
        env: Mapping[str, str] | None = None,
        llm_transport: httpx.AsyncBaseTransport | None = None,
        tool_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        config.ensure_dirs()
        self.config = config
        self.env = os.environ if env is None else env
        self.store = Store(config.db_path)
        self.registry = build_registry(config)
        self.mcp = McpManager(config.mcp_config, self.registry)
        self.http = httpx.AsyncClient(transport=tool_transport, timeout=20.0)
        self.llm_transport = llm_transport
        self.prompt_mode_models: set[str] = set()
        self.busy: set[str] = set()  # Conversations with a run in progress.
        self.listeners: set[Callable[[dict[str, Any]], Awaitable[None]]] = set()
        self.scheduler = Scheduler(self)
        self._provider: Provider | None = None
        self._provider_key: tuple[str, str, str] | None = None
        self._provider_lock = asyncio.Lock()

    async def start(self, *, background: bool = False) -> None:
        """Connect MCP servers. ``background`` also starts automations (server mode only)."""
        await self.mcp.start()
        if background:
            self.scheduler.start()

    async def aclose(self) -> None:
        await self.scheduler.stop()
        await self.mcp.stop()
        if self._provider:
            await self._provider.aclose()
        await self.http.aclose()
        self.store.close()

    # Preferences ----------------------------------------------------------------------------

    def preferences(self) -> tuple[Preferences, set[str]]:
        return resolve_preferences(self.store.get_preferences(), self.env)

    def update_preferences(self, changes: dict[str, Any]) -> tuple[Preferences, set[str]]:
        """Validate and persist changes. Keys locked by the environment are ignored."""
        current, locked = self.preferences()
        changes = {
            k: v for k, v in changes.items() if k in Preferences.model_fields and k not in locked
        }
        if "base_url" in changes:
            changes["base_url"] = str(changes["base_url"]).strip().rstrip("/")
        moved = any(
            k in changes and changes[k] != getattr(current, k) for k in ("provider", "base_url")
        )
        if moved and "api_key" not in changes:
            changes["api_key"] = ""  # Never send a saved key to a different server.
        try:
            Preferences.model_validate({**current.model_dump(), **changes})
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first["loc"])
            raise ValueError(f"{field}: {first['msg']}") from exc
        self.store.set_preferences(changes)
        return self.preferences()

    # Provider -------------------------------------------------------------------------------

    async def provider(self) -> Provider:
        prefs, _ = self.preferences()
        key = (prefs.provider, prefs.base_url, prefs.api_key)
        async with self._provider_lock:
            if self._provider is None or self._provider_key != key:
                if self._provider:
                    await self._provider.aclose()
                self._provider = await create_provider(
                    prefs.provider, prefs.base_url, prefs.api_key, transport=self.llm_transport
                )
                self._provider_key = key
                self.prompt_mode_models.clear()
            return self._provider

    async def resolve_model(self, provider: Provider, prefs: Preferences) -> str:
        if prefs.model:
            return prefs.model
        models = await provider.list_models()
        if not models:
            hint = (
                "Download one in Settings → Model, or run `ollama pull qwen3:4b`."
                if provider.supports_pull
                else "Load a model in your model server."
            )
            raise LLMError("No models are installed.", hint=hint)
        # Prefer a model that can call tools; capability lookups are cached per model.
        for model in models[:8]:
            if (await provider.capabilities(model.name)).tools:
                return model.name
        return models[0].name

    async def health(self) -> dict[str, Any]:
        prefs, _ = self.preferences()
        info: dict[str, Any] = {
            "ok": False,
            "provider": prefs.provider,
            "base_url": prefs.base_url,
            "model": prefs.model,
        }
        try:
            provider = await self.provider()
            info["provider"] = provider.kind
            info["version"] = await provider.version()
            models = await provider.list_models()
            info["models"] = len(models)
            info["model"] = prefs.model or (
                await self.resolve_model(provider, prefs) if models else ""
            )
            if info["model"]:
                caps = await provider.capabilities(info["model"])
                info["capabilities"] = caps.__dict__
                info["installed"] = any(m.name == info["model"] for m in models) or not models
            info["ok"] = True
        except LLMError as exc:
            info["error"] = exc.message
            info["hint"] = exc.hint
        return info

    # Events to every open window ------------------------------------------------------------

    async def broadcast(self, event: dict[str, Any]) -> int:
        delivered = 0
        for listener in list(self.listeners):
            with contextlib.suppress(Exception):
                await listener(event)
                delivered += 1
        return delivered

    async def notify(
        self, title: str, body: str = "", *, conversation_id: str | None = None, level: str = "info"
    ) -> int:
        """Show a notification in every open window. Marks the chat unread if nobody sees it."""
        delivered = await self.broadcast(
            {
                "type": "notification",
                "title": title,
                "body": body,
                "conversation_id": conversation_id,
                "level": level,
            }
        )
        if conversation_id and not delivered:
            self.store.set_unread(conversation_id, True)
        return delivered

    def tool_context(self, conversation_id: str | None = None) -> ToolContext:
        return ToolContext(
            config=self.config,
            store=self.store,
            http=self.http,
            conversation_id=conversation_id,
            runtime=self,
        )
