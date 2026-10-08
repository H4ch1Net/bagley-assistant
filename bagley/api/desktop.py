"""Endpoints for clients outside the browser: the Quickshell overlay and bar, the shell, the
phone and the voice loop.

* ``POST /api/ask`` runs one turn and streams its events as NDJSON (one JSON object a line).
* ``GET /api/activity`` and ``/api/activity/stream`` report what Bagley is doing.
* ``GET /api/approvals`` and ``POST /api/approvals/{id}`` answer pending approvals.
* ``POST /api/captures`` stores an image for a vision model; ``GET`` serves it back.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from bagley import notify
from bagley.agent import Agent, RunRequest
from bagley.api import runtime
from bagley.approvals import Pending, summarize
from bagley.llm import ToolCall
from bagley.runtime import Runtime
from bagley.tools import Tool

router = APIRouter()

MAX_IMAGE_BYTES = 12_000_000
IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"RIFF": ".webp",
    b"GIF8": ".gif",
}
KEEPALIVE = 20.0
Source = Literal["api", "cli", "overlay", "shell", "voice", "phone", "web", "routine"]


def image_suffix(data: bytes) -> str | None:
    for magic, suffix in IMAGE_MAGIC.items():
        if data.startswith(magic):
            if suffix == ".webp" and data[8:12] != b"WEBP":
                return None
            return suffix
    return None


def save_capture(rt: Runtime, data: bytes) -> str:
    """Store an image under the captures folder and return its file name."""
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"Images up to {MAX_IMAGE_BYTES // 1_000_000} MB are accepted.")
    suffix = image_suffix(data)
    if not suffix:
        raise HTTPException(415, "Only PNG, JPEG, WebP and GIF images are accepted.")
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{suffix}"
    (rt.captures_dir / name).write_bytes(data)
    return name


def context_block(context: dict[str, str]) -> str:
    """What the user is looking at, as a block appended to their question."""
    labels = {
        "window_title": "Focused window",
        "app": "Application",
        "selection": "Selected text",
        "clipboard": "Clipboard",
        "cwd": "Working directory",
        "command": "Last command",
        "output": "Output",
    }
    lines = []
    for key, label in labels.items():
        value = (context.get(key) or "").strip()
        if not value:
            continue
        value = value[:8000]
        lines.append(
            f"{label}:\n{value}" if "\n" in value or len(value) > 80 else f"{label}: {value}"
        )
    if not lines:
        return ""
    return "\n\n<context>\n" + "\n".join(lines) + "\n</context>"


class ImageIn(BaseModel):
    data: str = Field(min_length=8)  # Base64.


class AskBody(BaseModel):
    text: str = Field(default="", max_length=100_000)
    conversation_id: str | None = Field(default=None, max_length=40)
    source: Source = "api"
    images: list[ImageIn] = Field(default_factory=list, max_length=4)
    captures: list[str] = Field(default_factory=list, max_length=4)  # Already uploaded.
    context: dict[str, str] = Field(default_factory=dict)
    approvals: Literal["ask", "deny"] = "ask"
    notify: bool = False  # Also show the reply as a desktop notification (mako).
    mode: str | None = Field(default=None, max_length=32)  # Mode for a new conversation.
    tools: bool = True
    detach: bool = False  # Keep running if the client goes away.


async def ask_events(
    rt: Runtime, body: AskBody, images: list[str]
) -> tuple[asyncio.Task[None], asyncio.Queue[dict[str, Any] | None]]:
    """Start a run and return its task and the queue its events arrive on (None at the end)."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    current = {"conversation_id": body.conversation_id}

    async def emit(event: dict[str, Any]) -> None:
        if event["type"] == "run.start":
            current["conversation_id"] = event["conversation_id"]
        await queue.put(event)

    async def approve(call: ToolCall, tool: Tool) -> bool:
        if body.approvals == "deny":
            return False
        decision = await rt.approvals.request(
            Pending(
                id=call.id,
                tool=tool.name,
                arguments=call.arguments,
                summary=summarize(tool.summary, tool.name, call.arguments),
                conversation_id=current["conversation_id"],
                source=body.source,
            )
        )
        return decision in ("allow", "always")

    text = body.text.strip() or ("What's on my screen?" if images else "")
    meta: dict[str, Any] = {}
    if body.context:
        meta["context"] = {k: v[:500] for k, v in body.context.items() if isinstance(v, str)}
    request = RunRequest(
        text=text + context_block(body.context),
        conversation_id=body.conversation_id,
        source=body.source,
        images=images,
        conversation_mode=body.mode,
        tools=body.tools,
        user_meta=meta,
    )

    async def run() -> None:
        try:
            await Agent(rt).run(request, emit, approve)
        finally:
            await queue.put(None)

    return asyncio.create_task(run()), queue


@router.post("/api/ask")
async def ask(body: AskBody, request: Request) -> StreamingResponse:
    rt = runtime(request)
    names: list[str] = []
    for image in body.images:
        try:
            raw = base64.b64decode(image.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(400, "Images must be base64.") from exc
        names.append(save_capture(rt, raw))
    for name in body.captures:
        if not (rt.captures_dir / name).is_file() or "/" in name or "\\" in name:
            raise HTTPException(404, f"Capture {name} not found.")
        names.append(name)
    if not body.text.strip() and not names:
        raise HTTPException(422, "Ask something.")
    task, queue = await ask_events(rt, body, names)

    async def stream() -> AsyncIterator[bytes]:
        reply: list[str] = []
        machine = ""
        finished = False
        try:
            while (event := await queue.get()) is not None:
                if event["type"] == "text.delta":
                    reply.append(event["text"])
                elif event["type"] == "text.retract":
                    joined = "".join(reply)
                    reply = [joined[: len(joined) - int(event.get("chars") or 0)]]
                elif event["type"] == "model":
                    machine = event.get("machine", "")
                elif event["type"] == "message" and reply and not reply[-1].endswith("\n\n"):
                    reply.append("\n\n")
                yield (json.dumps(event, default=str) + "\n").encode()
            finished = True
        finally:
            if not finished and not body.detach and not task.done():
                task.cancel()
        text = "".join(reply).strip()
        if body.notify and text and notify.desktop_available():
            await notify.send_desktop(
                notify.Note(f"BAGLEY // {machine}" if machine else "BAGLEY", _plain(text))
            )

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _plain(markdown: str, limit: int = 500) -> str:
    import re

    text = re.sub(r"```.*?```", "[code]", markdown, flags=re.S)
    text = re.sub(r"[*_`#>|]", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Activity ---------------------------------------------------------------------------------------


@router.get("/api/activity")
async def activity(request: Request) -> dict[str, Any]:
    return runtime(request).activity.snapshot()


@router.get("/api/activity/stream")
async def activity_stream(request: Request) -> StreamingResponse:
    rt = runtime(request)
    queue = rt.activity.subscribe()

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield (json.dumps(rt.activity.snapshot()) + "\n").encode()
            while True:
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield b'{"type":"ping"}\n'
                    continue
                yield (json.dumps(snap) + "\n").encode()
        finally:
            rt.activity.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# Approvals --------------------------------------------------------------------------------------


class DecisionBody(BaseModel):
    decision: Literal["allow", "deny", "always"]


@router.get("/api/approvals")
async def list_approvals(request: Request) -> list[dict[str, Any]]:
    return runtime(request).approvals.pending()


@router.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, body: DecisionBody, request: Request) -> dict[str, Any]:
    if not runtime(request).approvals.resolve(approval_id, body.decision):
        raise HTTPException(404, "Nothing is waiting for that approval.")
    return {"ok": True}


# Captures ---------------------------------------------------------------------------------------


@router.post("/api/captures", status_code=201)
async def upload_capture(request: Request) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Image too large.")
    rt = runtime(request)
    name = save_capture(rt, bytes(body))
    return {"name": name, "url": f"/api/captures/{name}", "size": len(body)}


@router.get("/api/captures/{name}")
async def get_capture(name: str, request: Request) -> FileResponse:
    rt = runtime(request)
    from bagley.agent import CAPTURE_NAME

    path = rt.captures_dir / name
    if not CAPTURE_NAME.match(name) or not path.is_file():
        raise HTTPException(404, "Not found.")
    types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    types |= {".webp": "image/webp", ".gif": "image/gif"}
    with contextlib.suppress(KeyError):
        return FileResponse(
            path,
            media_type=types[path.suffix.lower()],
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )
    raise HTTPException(404, "Not found.")
