"""The audit log: every tool call, who allowed it and how it went."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from bagley.api import runtime
from bagley.modes import public_modes

router = APIRouter()


@router.get("/api/audit")
async def audit(
    request: Request, limit: int = 200, after: int = 0, tool: str = "", conversation: str = ""
) -> list[dict[str, Any]]:
    return runtime(request).store.list_audit(
        limit, after=after, tool=tool, conversation_id=conversation
    )


@router.get("/api/modes")
async def modes(request: Request) -> list[dict[str, Any]]:
    prefs, _ = runtime(request).preferences()
    return public_modes(prefs)
