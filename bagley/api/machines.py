"""Machines Bagley can route to, with their health, and the routing decision for a request."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from bagley.api import runtime
from bagley.llm import LLMError, create_provider, presets

router = APIRouter()


@router.get("/api/machines")
async def machines(request: Request, fresh: bool = False) -> dict[str, Any]:
    rt = runtime(request)
    prefs, _ = rt.preferences()
    return {
        "routing": prefs.routing,
        "light_local": prefs.light_local,
        "machines": await rt.router.status(prefs, fresh=fresh),
    }


@router.get("/api/machines/route")
async def route(request: Request, purpose: str = "chat") -> dict[str, Any]:
    """Which machine and model would answer now."""
    if purpose not in ("chat", "light", "vision"):
        raise HTTPException(422, "purpose must be chat, light or vision.")
    rt = runtime(request)
    prefs, _ = rt.preferences()
    try:
        chosen = await rt.router.choose(prefs, purpose)  # type: ignore[arg-type]
    except LLMError as exc:
        return {"ok": False, "error": exc.message, "hint": exc.hint}
    return {"ok": True, **chosen.describe()}


@router.get("/api/machines/{machine_id}/models")
async def machine_models(machine_id: str, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    prefs, _ = rt.preferences()
    machine = rt.router.find(prefs, machine_id)
    if machine is None:
        raise HTTPException(404, "No such machine.")
    try:
        provider = await rt.router.provider(machine)
        models = await provider.list_models()
        loaded = await provider.loaded() if provider.supports_pull else []
    except LLMError as exc:
        raise HTTPException(502, exc.message) from exc
    return {
        "machine": machine.public(),
        "models": [m.to_dict() for m in models],
        "loaded": loaded,
    }


@router.get("/api/machines/presets")
async def machine_presets(request: Request) -> list[dict[str, Any]]:
    """Hosted APIs with their URL, key variable and suggested models (Claude first)."""
    return presets.public(runtime(request).env)


class TestBody(BaseModel):
    provider: str = Field("auto", pattern=r"^(auto|ollama|openai|anthropic)$")
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field("", max_length=1000)
    id: str = Field("", max_length=32)  # A saved machine whose key to use when none is given.


@router.post("/api/machines/test")
async def test_machine(body: TestBody, request: Request) -> dict[str, Any]:
    """Connect with settings that aren't saved yet and list the models there."""
    rt = runtime(request)
    prefs, _ = rt.preferences()
    key = body.api_key.strip()
    if not key and body.id:
        saved = next((m for m in prefs.machines if m.id == body.id), None)
        key = saved.api_key if saved else ""
    provider = await create_provider(
        body.provider, body.base_url.strip(), key, transport=rt.llm_transport, env=rt.env
    )
    try:
        models = await provider.list_models()
    except LLMError as exc:
        return {"ok": False, "kind": provider.kind, "error": exc.message, "hint": exc.hint}
    finally:
        await provider.aclose()
    return {"ok": True, "kind": provider.kind, "models": [m.to_dict() for m in models]}
