"""Shared application state: config, storage, tools and the active model provider."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from bagley import notify as notifications
from bagley.activity import Activity
from bagley.approvals import ApprovalBroker
from bagley.automations import Scheduler
from bagley.config import Preferences, ServerConfig, resolve_preferences
from bagley.knowledge import KnowledgeBase, is_embedding_model
from bagley.llm import LLMError, Provider, create_provider
from bagley.mcp import McpManager
from bagley.routing import Router
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
        self.knowledge = KnowledgeBase(self)
        self.router = Router(self)
        self.approvals = ApprovalBroker(self)
        self.activity = Activity(self)
        self.services: dict[str, Any] = {}  # Feature singletons, created on first use.
        self._notify_tasks: set[asyncio.Task[Any]] = set()
        self._provider: Provider | None = None
        self._provider_key: tuple[str, str, str] | None = None
        self._provider_lock = asyncio.Lock()

    async def start(self, *, background: bool = False) -> None:
        """Connect MCP servers. ``background`` also starts automations (server mode only)."""
        await self.mcp.start()
        if background:
            self.scheduler.start()
            self.knowledge.start()

    async def aclose(self) -> None:
        await self.scheduler.stop()
        await self.knowledge.stop()
        self.knowledge.close()
        await self.mcp.stop()
        await self.approvals.aclose()
        for task in list(self._notify_tasks):
            task.cancel()
        await asyncio.gather(*self._notify_tasks, return_exceptions=True)
        for service in self.services.values():
            close = getattr(service, "aclose", None)
            if close:
                with contextlib.suppress(Exception):
                    await close()
        await self.router.aclose()
        if self._provider:
            await self._provider.aclose()
        await self.http.aclose()
        self.store.close()

    @property
    def captures_dir(self) -> Path:
        """Screenshots and images sent to vision models."""
        path = Path(self.config.data_dir) / "captures"
        path.mkdir(parents=True, exist_ok=True)
        return path

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
        if "machines" in changes:
            changes["machines"] = self._merge_machine_keys(current, changes["machines"])
        try:
            Preferences.model_validate({**current.model_dump(), **changes})
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first["loc"])
            raise ValueError(f"{field}: {first['msg']}") from exc
        self.store.set_preferences(changes)
        return self.preferences()

    @staticmethod
    def _merge_machine_keys(current: Preferences, machines: Any) -> Any:
        """The browser never sees machine API keys, so it sends them back empty. Keep a saved
        key while the machine's server and URL stay the same; drop it when they change."""
        if not isinstance(machines, list):
            return machines
        saved = {m.id: m for m in current.machines}
        out = []
        for item in machines:
            if not isinstance(item, dict):
                out.append(item)
                continue
            item = dict(item)
            item.pop("has_api_key", None)
            old = saved.get(str(item.get("id")))
            same_place = (
                old is not None
                and str(item.get("base_url", "")).rstrip("/") == old.base_url.rstrip("/")
                and item.get("provider", "auto") == old.provider
            )
            if not item.get("api_key") and same_place and old is not None:
                item["api_key"] = old.api_key
            out.append(item)
        return out

    # Provider -------------------------------------------------------------------------------

    async def provider(self) -> Provider:
        prefs, _ = self.preferences()
        key = (prefs.provider, prefs.base_url, prefs.api_key)
        async with self._provider_lock:
            if self._provider is None or self._provider_key != key:
                if self._provider:
                    await self._provider.aclose()
                self._provider = await create_provider(
                    prefs.provider,
                    prefs.base_url,
                    prefs.api_key,
                    transport=self.llm_transport,
                    env=self.env,
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
        models = [m for m in models if not is_embedding_model(m.name)] or models
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
        self,
        title: str,
        body: str = "",
        *,
        conversation_id: str | None = None,
        level: str = "info",
        desktop: bool | None = None,
        tags: list[str] | None = None,
    ) -> int:
        """Show a notification in every open window, and on the desktop and phone as the
        preferences allow. Marks the chat unread if no window sees it.

        ``level`` is "info", "important", "critical" or "error" (shown as an error toast and
        sent as important). ``desktop`` forces the desktop notification on or off."""
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
        prefs, _ = self.preferences()
        if prefs.desktop_notifications or prefs.ntfy_url or desktop:
            note = notifications.Note(
                title,
                body,
                level="important" if level == "error" else level,
                url=self.link(conversation_id),
                tags=tags or (["warning"] if level in ("error", "critical") else []),
            )
            task = asyncio.create_task(
                notifications.fan_out(prefs, self.http, note, desktop=desktop)
            )
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)
        return delivered

    def link(self, conversation_id: str | None = None) -> str:
        """A URL that opens Bagley (at a chat), for notification clicks. Uses BAGLEY_PUBLIC_URL
        when set, e.g. the Tailscale address the phone reaches."""
        base = (self.env.get("BAGLEY_PUBLIC_URL") or "").rstrip("/")
        if not base:
            host = "127.0.0.1" if self.config.host in ("0.0.0.0", "::") else self.config.host
            base = f"http://{host}:{self.config.port}"
        return f"{base}/#/c/{conversation_id}" if conversation_id else f"{base}/"

    def tool_context(self, conversation_id: str | None = None) -> ToolContext:
        return ToolContext(
            config=self.config,
            store=self.store,
            http=self.http,
            conversation_id=conversation_id,
            runtime=self,
        )
