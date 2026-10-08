"""Voice endpoints: server-side speech for the web UI and the desktop daemon.

* ``POST /api/tts`` ``{text}`` returns audio (``audio/wav`` from Piper, ``audio/mpeg`` from
  ElevenLabs), or 409 when speech is left to the browser or the engine isn't set up.
* ``POST /api/stt`` takes raw audio (the request's Content-Type) and returns ``{text}``.
* ``GET /api/voice/status`` reports engines, programs, voices and models found.
* ``POST /api/voice/speaking`` ``{speaking}`` lets a client that plays audio show VOICE on the
  avatar.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from bagley.api import runtime
from bagley.runtime import Runtime
from bagley.voice import VoiceError, VoiceUnavailable, status, stt, tts

router = APIRouter()


class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)  # Only the first 2500 spoken chars count.


class SpeakingBody(BaseModel):
    speaking: bool
    seconds: float = Field(120.0, gt=0, le=900)  # Reset by itself if the client never says so.


class SpeakingTimer:
    """Clears the speaking flag when a client that set it goes quiet."""

    def __init__(self) -> None:
        self.handle: asyncio.TimerHandle | None = None
        self.tasks: set[asyncio.Task[None]] = set()

    def start(self, rt: Runtime, seconds: float) -> None:
        loop = asyncio.get_running_loop()

        def expire() -> None:
            self.handle = None
            task = loop.create_task(rt.activity.set_speaking(False))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        self.cancel()
        self.handle = loop.call_later(seconds, expire)

    def cancel(self) -> None:
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None

    async def aclose(self) -> None:
        self.cancel()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@router.post("/api/tts")
async def speak(body: SpeakBody, request: Request) -> Response:
    rt = runtime(request)
    try:
        audio, mime = await tts.synthesize(rt, body.text)
    except VoiceUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except VoiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(
        audio,
        media_type=mime,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/api/stt")
async def transcribe(request: Request) -> dict[str, Any]:
    rt = runtime(request)
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime and not (mime.startswith(("audio/", "video/")) or mime == "application/octet-stream"):
        raise HTTPException(415, "Send the recording as the request body with an audio type.")
    body = bytearray()
    async for part in request.stream():
        body += part
        if len(body) > stt.MAX_AUDIO_BYTES:
            raise HTTPException(413, f"Audio up to {stt.MAX_AUDIO_BYTES // 1_000_000} MB.")
    if not body:
        raise HTTPException(422, "No audio.")
    try:
        text = await stt.transcribe(rt, bytes(body), mime)
    except VoiceUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except VoiceError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"text": text}


@router.get("/api/voice/status")
async def voice_status(request: Request) -> dict[str, Any]:
    rt = runtime(request)
    prefs, _ = rt.preferences()
    return await asyncio.to_thread(status, prefs, rt.config.data_dir)


@router.post("/api/voice/speaking")
async def speaking(body: SpeakingBody, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    timer: SpeakingTimer = rt.services.setdefault("voice.speaking", SpeakingTimer())
    timer.cancel()
    await rt.activity.set_speaking(body.speaking)
    if body.speaking:
        timer.start(rt, body.seconds)
    return {"speaking": body.speaking}
