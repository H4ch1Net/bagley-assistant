"""Bagley's voice: speech out (Piper or ElevenLabs), speech in (whisper.cpp) and the wake word
daemon behind ``bagley listen``.

* ``text``: Markdown to speakable sentences, plus a light in-character pass.
* ``tts``: ``synthesize(rt, text)`` with the engine chosen in the preferences.
* ``stt``: ``transcribe(rt, audio, mime)`` with whisper.cpp, converting with ffmpeg.
* ``audio``: WAV helpers, the ready tone and playback.
* ``wake``: the energy VAD and wake word matching (pure Python, testable).
* ``listen``: the desktop daemon that ties them together.

Programs that run to completion (piper, ffmpeg, whisper.cpp, players) go through a ``Runner``
(``run_program`` by default) so tests can fake them: argument lists, never a shell, timeouts and
output limits. The microphone is the one long-running process (``listen.microphone``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.config import Preferences

MAX_OUTPUT = 64_000_000  # Bytes of stdout kept from a program (minutes of audio).


class VoiceError(Exception):
    """A voice operation failed; the message is meant for the user."""


class VoiceUnavailable(VoiceError):
    """The engine is not chosen or not set up (missing program, voice or model)."""


# Runs ``args`` with ``stdin`` and returns (exit code, stdout, stderr).
Runner = Callable[[list[str], bytes | None, float], Awaitable[tuple[int, bytes, bytes]]]


async def run_program(
    args: list[str], stdin: bytes | None = None, timeout: float = 60.0
) -> tuple[int, bytes, bytes]:
    """Run a program without a shell. Kills it (and anything it started) on timeout."""
    group = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **group,
        )
    except OSError as exc:
        raise VoiceUnavailable(f"Could not start {Path(args[0]).name}: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError as exc:
        _kill(proc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2)
        raise VoiceError(f"{Path(args[0]).name} timed out after {timeout:.0f}s.") from exc
    except asyncio.CancelledError:
        _kill(proc)
        raise
    return proc.returncode or 0, out[:MAX_OUTPUT], err[-4000:]


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, OSError):
        if sys.platform == "win32":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # Its own session: the group id is its pid.


def find_program(*names: str) -> str | None:
    """The first of ``names`` on PATH."""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def last_line(stderr: bytes) -> str:
    """The last meaningful line a program printed, for error messages."""
    lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "no error output"


def status(prefs: Preferences, data_dir: Path) -> dict[str, Any]:
    """What is installed and chosen, for ``GET /api/voice/status`` and ``bagley voices``."""
    from bagley.voice import audio, stt, tts

    piper = tts.piper_binary()
    voices = tts.find_voices(data_dir)
    voice = tts.resolve_voice(prefs.piper_voice, data_dir)
    whisper = stt.whisper_binary()
    models = stt.find_models(data_dir)
    model = stt.resolve_model(prefs.whisper_model, data_dir)
    ffmpeg = find_program("ffmpeg")
    problems: list[str] = []
    if prefs.piper_voice and not voice:
        problems.append(f"Piper voice not found: {prefs.piper_voice}")
    if prefs.whisper_model and not model:
        problems.append(f"Whisper model not found: {prefs.whisper_model}")
    tts_ready = False
    if prefs.tts_engine == "piper":
        tts_ready = bool(piper and voice)
        if not piper:
            problems.append("Piper is not installed (piper-tts).")
        elif not voice:
            problems.append("No Piper voice found. Download en_GB-alan-medium.")
    elif prefs.tts_engine == "elevenlabs":
        voice_ok = not prefs.elevenlabs_voice or bool(tts.VOICE_ID.match(prefs.elevenlabs_voice))
        tts_ready = bool(prefs.elevenlabs_key) and voice_ok
        if not prefs.elevenlabs_key:
            problems.append("Add an ElevenLabs API key.")
        if not voice_ok:
            problems.append("An ElevenLabs voice id is letters and digits only.")
    stt_ready = bool(whisper and model)
    if prefs.stt_engine == "whisper":
        if not whisper:
            problems.append("whisper.cpp is not installed (whisper-cli).")
        elif not model:
            problems.append("No whisper model found. Download ggml-base.en.bin.")
        if not ffmpeg:
            problems.append("ffmpeg is needed to read audio from the browser.")
    return {
        "tts_engine": prefs.tts_engine,
        "stt_engine": prefs.stt_engine,
        "wake_word": prefs.wake_word,
        "tts_ready": tts_ready,
        "stt_ready": stt_ready,
        "piper": {
            "binary": piper,
            "voice": str(voice) if voice else None,
            "voices": voices,
            "recommended": tts.RECOMMENDED_VOICES,
        },
        "whisper": {
            "binary": whisper,
            "model": str(model) if model else None,
            "models": [str(m) for m in models],
        },
        "ffmpeg": ffmpeg,
        "elevenlabs": {
            "configured": bool(prefs.elevenlabs_key),
            "voice": prefs.elevenlabs_voice or tts.DEFAULT_ELEVENLABS_VOICE,
            "model": tts.ELEVENLABS_MODEL,
        },
        "players": list(dict.fromkeys(audio.players("audio/wav") + audio.players("audio/mpeg"))),
        "recorders": [name for name in ("pw-record", "parecord", "arecord") if find_program(name)],
        "problems": problems,
    }
