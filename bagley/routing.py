"""Route each request to a machine: a GPU desktop over Tailscale, this laptop, or a hosted API.

The machine Bagley runs on ("local") is set by the top-level model settings. Other machines are
listed in ``Preferences.machines``. With routing on "auto", heavy work goes to the first GPU
machine that answers, then this machine, then any other local machines, then cloud fallbacks.
Light work (chat titles, shell one-liners) stays on this machine when ``light_local`` is on.
A machine that fails is skipped for a short while and the next one takes over; the answer
carries the name of the machine that produced it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from bagley.llm import LLMError, Provider, create_provider

if TYPE_CHECKING:
    from bagley.config import Preferences
    from bagley.runtime import Runtime

log = logging.getLogger("bagley.routing")

Purpose = Literal["chat", "light", "vision"]
HEALTH_TTL = 30.0  # Seconds a successful probe is trusted.
DOWN_FOR = 45.0  # Seconds a failing machine is skipped.
PROBE_TIMEOUT = 3.0
VISION_HINTS = ("vl", "vision", "llava", "gemma3", "minicpm-v", "moondream", "pixtral", "llama4")


def hostname() -> str:
    return (socket.gethostname().split(".")[0] or "LOCAL").upper()[:16]


@dataclass(frozen=True)
class MachineSpec:
    id: str
    name: str
    role: str
    provider: str
    base_url: str
    api_key: str = ""
    model: str = ""
    vision_model: str = ""

    @property
    def is_local(self) -> bool:
        return self.id == "local"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "vision_model": self.vision_model,
        }


@dataclass
class Route:
    machine: MachineSpec
    provider: Provider
    model: str
    purpose: str = "chat"

    def describe(self) -> dict[str, Any]:
        return {
            "machine": self.machine.name,
            "machine_id": self.machine.id,
            "role": self.machine.role,
            "model": self.model,
        }


class NoRouteError(LLMError):
    pass


class Router:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self._providers: dict[tuple[str, str, str], Provider] = {}
        self._health: dict[str, tuple[float, dict[str, Any]]] = {}
        self._down_until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # Machines -------------------------------------------------------------------------------

    def machines(self, prefs: Preferences, *, enabled_only: bool = True) -> list[MachineSpec]:
        local = MachineSpec(
            id="local",
            name=(prefs.machine_name or hostname()).upper(),
            role="local",
            provider=prefs.provider,
            base_url=prefs.base_url,
            api_key=prefs.api_key,
            model=prefs.model,
            vision_model=prefs.vision_model,
        )
        others = [
            MachineSpec(
                id=m.id,
                name=m.name.upper(),
                role=m.role,
                provider=m.provider,
                base_url=m.base_url.rstrip("/"),
                api_key=m.api_key,
                model=m.model,
                vision_model=m.vision_model,
            )
            for m in prefs.machines
            if m.enabled or not enabled_only
        ]
        return [local, *others]

    def order(self, prefs: Preferences, purpose: Purpose = "chat") -> list[MachineSpec]:
        machines = self.machines(prefs)
        local, others = machines[0], machines[1:]
        if prefs.routing == "local":
            return [local]
        if prefs.routing != "auto":
            pinned = [m for m in others if m.id == prefs.routing]
            return pinned or [local]
        gpu = [m for m in others if m.role == "gpu"]
        nearby = [m for m in others if m.role == "local"]
        cloud = [m for m in others if m.role == "cloud"]
        if purpose == "light" and prefs.light_local:
            return [local, *gpu, *nearby, *cloud]
        return [*gpu, local, *nearby, *cloud]

    def find(self, prefs: Preferences, machine_id: str) -> MachineSpec | None:
        return next((m for m in self.machines(prefs) if m.id == machine_id), None)

    async def provider(self, machine: MachineSpec) -> Provider:
        if machine.is_local:
            return await self.rt.provider()
        key = (machine.provider, machine.base_url, machine.api_key)
        async with self._lock:
            provider = self._providers.get(key)
            if provider is None:
                provider = await create_provider(
                    machine.provider,
                    machine.base_url,
                    machine.api_key,
                    transport=self.rt.llm_transport,
                )
                self._providers[key] = provider
            return provider

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
        self._providers.clear()

    # Health ---------------------------------------------------------------------------------

    def mark_down(self, machine: MachineSpec) -> None:
        self._down_until[machine.id] = time.monotonic() + DOWN_FOR
        self._health.pop(machine.id, None)

    def is_down(self, machine: MachineSpec) -> bool:
        return self._down_until.get(machine.id, 0) > time.monotonic()

    async def probe(self, machine: MachineSpec, *, fresh: bool = False) -> dict[str, Any]:
        cached = self._health.get(machine.id)
        if cached and not fresh and cached[0] > time.monotonic():
            return cached[1]
        started = time.monotonic()
        info: dict[str, Any] = {**machine.public(), "ok": False, "models": []}
        info.pop("base_url", None)
        try:
            provider = await self.provider(machine)
            models = await asyncio.wait_for(provider.list_models(), timeout=PROBE_TIMEOUT)
            info.update(
                ok=True,
                kind=provider.kind,
                models=[m.name for m in models],
                latency_ms=round((time.monotonic() - started) * 1000),
            )
            self._down_until.pop(machine.id, None)
            self._health[machine.id] = (time.monotonic() + HEALTH_TTL, info)
        except asyncio.TimeoutError:
            info["error"] = f"{machine.name} did not answer within {PROBE_TIMEOUT:g}s."
            self.mark_down(machine)
        except LLMError as exc:
            info["error"] = exc.message
            self.mark_down(machine)
        return info

    async def status(self, prefs: Preferences, *, fresh: bool = False) -> list[dict[str, Any]]:
        machines = self.machines(prefs, enabled_only=False)
        enabled = {m.id for m in self.machines(prefs)}
        probes = await asyncio.gather(
            *(self.probe(m, fresh=fresh) if m.id in enabled else _disabled(m) for m in machines)
        )
        order = [m.id for m in self.order(prefs)]
        for info in probes:
            info["enabled"] = info["id"] in enabled
            info["priority"] = order.index(info["id"]) if info["id"] in order else None
        return list(probes)

    # Choosing -------------------------------------------------------------------------------

    async def choose(
        self,
        prefs: Preferences,
        purpose: Purpose = "chat",
        *,
        exclude: set[str] | frozenset[str] = frozenset(),
    ) -> Route:
        candidates = [m for m in self.order(prefs, purpose) if m.id not in exclude]
        if not candidates:
            raise NoRouteError(
                "No machine is available to answer.",
                hint="Check the machines in Settings → Model, or switch routing to Automatic.",
            )
        if len(candidates) == 1 and not exclude:
            # A single machine: no probing, so errors read exactly as the server reports them.
            machine = candidates[0]
            provider = await self.provider(machine)
            return Route(machine, provider, await self.model_for(machine, provider, prefs, purpose))
        reasons: list[str] = []
        live = [m for m in candidates if not self.is_down(m)] or candidates
        for machine in live:
            health = await self.probe(machine)
            if not health["ok"]:
                reasons.append(f"{machine.name}: {health.get('error', 'offline')}")
                continue
            provider = await self.provider(machine)
            try:
                model = await self.model_for(machine, provider, prefs, purpose)
            except LLMError as exc:
                reasons.append(f"{machine.name}: {exc.message}")
                continue
            return Route(machine, provider, model, purpose)
        raise NoRouteError(
            "None of your machines can answer right now.",
            hint=" · ".join(reasons) or "Start a model server, or check Settings → Model.",
        )

    async def model_for(
        self, machine: MachineSpec, provider: Provider, prefs: Preferences, purpose: Purpose
    ) -> str:
        if purpose == "vision":
            return await self._vision_model(machine, provider, prefs)
        if machine.is_local:
            return await self.rt.resolve_model(provider, prefs)
        if machine.model:
            return machine.model
        from bagley.knowledge import is_embedding_model

        models = [m for m in await provider.list_models() if not is_embedding_model(m.name)]
        if not models:
            raise LLMError(f"{machine.name} has no models installed.")
        for model in models[:8]:
            if (await provider.capabilities(model.name)).tools:
                return model.name
        return models[0].name

    async def _vision_model(
        self, machine: MachineSpec, provider: Provider, prefs: Preferences
    ) -> str:
        if machine.vision_model:
            return machine.vision_model
        preferred = machine.model or (prefs.model if machine.is_local else "")
        if preferred and (await provider.capabilities(preferred)).vision:
            return preferred
        models = await provider.list_models()
        guesses = sorted(models, key=lambda m: not any(h in m.name.lower() for h in VISION_HINTS))
        for model in guesses[:12]:
            if (await provider.capabilities(model.name)).vision:
                return model.name
        raise LLMError(
            f"{machine.name} has no model that can see images.",
            hint="Install one, e.g. `ollama pull qwen2.5vl:7b`, or set a vision model in "
            "Settings → Model.",
        )


async def _disabled(machine: MachineSpec) -> dict[str, Any]:
    info: dict[str, Any] = {**machine.public(), "ok": False, "models": [], "error": "disabled"}
    info.pop("base_url", None)
    return info
