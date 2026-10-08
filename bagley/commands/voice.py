"""``bagley speak``, ``bagley transcribe``, ``bagley listen`` and ``bagley voices``.

``speak`` and ``listen`` use the running server's voice (Piper or ElevenLabs) when it has one
and fall back to Piper on this computer. ``transcribe`` and ``listen`` run whisper.cpp here, or
on the server when this computer has none. Setup is in docs/voice.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import mimetypes
import os
import signal
import sys
from pathlib import Path
from typing import Any

from bagley.client import Client, ServerUnavailable
from bagley.config import Preferences, ServerConfig, resolve_preferences
from bagley.voice import VoiceError, VoiceUnavailable, status
from bagley.voice.listen import (
    Conversation,
    Listener,
    Options,
    Speaker,
    Transcriber,
    chime,
    meter,
    microphone,
    recorder_command,
)
from bagley.voice.stt import MAX_AUDIO_BYTES
from bagley.voice.wake import VAD

NO_VOICE = "No voice: set Piper or ElevenLabs in Settings, or install piper-tts and a voice."
NO_STT = "No speech recognition: install whisper.cpp and a model (see docs/voice.md)."
MAX_TEXT = 100_000


# The ctOS palette as 24-bit ANSI colours.
CTOS = {
    "gray": "122;122;122",
    "body": "202;202;202",
    "white": "255;255;255",
    "ok": "0;250;154",
    "error": "252;62;56",
}


class Ink:
    """ctOS colours for the terminal, off for NO_COLOR and pipes."""

    def __init__(self, stream: Any = None) -> None:
        stream = sys.stdout if stream is None else stream
        self.on = stream.isatty() and not os.environ.get("NO_COLOR")

    def paint(self, color: str, text: str) -> str:
        return f"\033[38;2;{CTOS[color]}m{text}\033[0m" if self.on else text

    def ok(self, text: str) -> str:
        return self.paint("ok", text)

    def crit(self, text: str) -> str:
        return self.paint("error", text)

    def white(self, text: str) -> str:
        return self.paint("white", text)

    def dim(self, text: str) -> str:
        return self.paint("gray", text)


def local_preferences() -> tuple[Preferences, ServerConfig]:
    """This computer's preferences, read from the database without starting a runtime."""
    from bagley.store import Store

    config = ServerConfig.from_env()
    stored: dict[str, Any] = {}
    if config.db_path.is_file():
        store = Store(config.db_path)
        try:
            stored = store.get_preferences()
        finally:
            store.close()
    prefs, _ = resolve_preferences(stored)
    return prefs, config


def _client(local: bool) -> Client | None:
    if local:
        return None
    client = Client()
    if client.available():
        return client
    client.close()
    return None


def _fail(message: str) -> int:
    err = Ink(sys.stderr)
    print(err.crit(f"[CRIT] {message}"), file=sys.stderr)
    return 1


# speak ------------------------------------------------------------------------------------------


def cmd_speak(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read(MAX_TEXT).strip()
    if not text:
        return _fail("Nothing to say. Pass text or pipe it in.")
    prefs, config = local_preferences()
    client = _client(args.local)
    speaker = Speaker(client, prefs, config.data_dir, voice=args.voice)

    async def run() -> int:
        try:
            if not args.out:
                return 0 if await speaker.say(text) else _fail(NO_VOICE)
            speaker.mode = await speaker.plan()
            if not speaker.mode:
                return _fail(NO_VOICE)
            audio, mime = await speaker.synthesize(text[:MAX_TEXT])
            Path(args.out).expanduser().write_bytes(audio)
            print(Ink().ok(f"[OK] {mime} {len(audio)} BYTES -> {args.out}"))
            return 0
        finally:
            await speaker.aclose()
            if client:
                client.close()

    try:
        return asyncio.run(run())
    except VoiceError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return 130


# transcribe -------------------------------------------------------------------------------------


def cmd_transcribe(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        return _fail(f"{args.file}: no such file.")
    if path.stat().st_size > MAX_AUDIO_BYTES:
        return _fail(f"Audio up to {MAX_AUDIO_BYTES // 1_000_000} MB is accepted.")
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    prefs, config = local_preferences()
    client = _client(args.local)
    transcriber = Transcriber(client, prefs, config.data_dir, model=args.model)
    transcriber.language = args.language

    async def run() -> str:
        if not await transcriber.plan():
            raise VoiceUnavailable(NO_STT)
        return await transcriber.audio(data, mime)

    try:
        text = asyncio.run(run())
    except VoiceError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return 130
    finally:
        if client:
            client.close()
    print(text)
    return 0


# voices -----------------------------------------------------------------------------------------


def cmd_voices(args: argparse.Namespace) -> int:
    prefs, config = local_preferences()
    info = status(prefs, config.data_dir)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    ink = Ink()
    na = "--N/A--"

    def row(label: str, value: str, mark: str = "") -> None:
        tag = {"ok": ink.ok("[OK]  "), "warn": ink.white("[WARN]"), "": "      "}[mark]
        print(f"{tag} {ink.dim(label.ljust(11))} {value}")

    def engine(label: str, name: str, ready: bool) -> None:
        if name == "browser":
            row(label, "BROWSER", "ok")
        else:
            row(
                label,
                f"{name.upper()}  {'READY' if ready else 'NOT READY'}",
                "ok" if ready else "warn",
            )

    print(ink.white("VOICE // THIS COMPUTER"))
    engine("SPEECH OUT", prefs.tts_engine, info["tts_ready"])
    engine("SPEECH IN", prefs.stt_engine, info["stt_ready"])
    row("WAKE WORD", prefs.wake_word.upper())
    row("PIPER", info["piper"]["binary"] or na)
    row("VOICE", info["piper"]["voice"] or na)
    row("WHISPER", info["whisper"]["binary"] or na)
    row("MODEL", info["whisper"]["model"] or na)
    row("FFMPEG", info["ffmpeg"] or na)
    row("ELEVENLABS", "KEY SET" if info["elevenlabs"]["configured"] else "NO KEY")
    row("PLAYERS", ", ".join(info["players"]) or na)
    row("RECORDERS", ", ".join(info["recorders"]) or na)
    voices = info["piper"]["voices"]
    if voices:
        print(ink.white("PIPER VOICES"))
        width = max(len(v["name"]) for v in voices)
        for v in voices:
            details = f"{v['language'] or '?'}  {v['quality'] or '?'}  {v['sample_rate']}HZ"
            print(f"       {v['name'].ljust(width)}  {ink.dim(details.upper())}  {v['path']}")
    else:
        print(ink.white("GET A BRITISH VOICE"))
        for v in info["piper"]["recommended"]:
            print(f"       {v['name']}  {ink.dim(v['description'])}")
            print(f"         {v['model_url']}")
            print(f"         {v['config_url']}")
    for problem in info["problems"]:
        print(f"{ink.white('[WARN]')} {problem}")
    return 0


# listen -----------------------------------------------------------------------------------------


def cmd_listen(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_listen(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(Ink().dim("\n[OK] STOPPED"))
        return 0
    except VoiceError as exc:
        return _fail(str(exc))


async def _listen(args: argparse.Namespace) -> int:
    ink = Ink()
    task = asyncio.current_task()
    if task is not None:
        with contextlib.suppress(NotImplementedError, RuntimeError, AttributeError):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    command = recorder_command(args.device)
    if command is None:
        raise VoiceUnavailable("No recorder found: install pipewire (pw-record) or alsa-utils.")
    if args.test_mic:
        print(ink.white(f"MIC // {Path(command[0]).name.upper()}  CTRL+C TO STOP"))
        await meter(microphone(command), _meter_line(ink))
        return 0

    prefs, config = local_preferences()
    client = Client()
    remote: dict[str, Any] = {}
    with contextlib.suppress(ServerUnavailable):
        remote = await asyncio.to_thread(client.get, "/api/voice/status")
    options = Options(
        wake_word=args.wake_word or remote.get("wake_word") or prefs.wake_word,
        once=args.once,
        no_wake=args.no_wake,
        verbose=args.verbose,
    )
    transcriber = Transcriber(client, prefs, config.data_dir, model=args.model)
    speaker = Speaker(client, prefs, config.data_dir, voice=args.voice)
    try:
        if not await transcriber.plan():
            raise VoiceUnavailable(NO_STT)
        speech = await speaker.plan()
        model = transcriber.model.name if transcriber.model else ""
        print(ink.white("BAGLEY // LISTEN"))
        print(f"{ink.dim('WAKE ')} {'OFF' if args.no_wake else options.wake_word.upper()}")
        print(f"{ink.dim('MIC  ')} {Path(command[0]).name} {args.device}".rstrip())
        stt_mode = f"LOCAL {model}" if transcriber.mode == "local" else "SERVER"
        print(f"{ink.dim('STT  ')} {stt_mode}")
        print(f"{ink.dim('TTS  ')} {speech.upper() or 'PRINT ONLY'}")
        print(ink.ok("[OK] LISTENING"), flush=True)

        def log(kind: str, text: str) -> None:
            if kind == "heard" and options.verbose:
                print(ink.dim(f'HEARD "{text}"'), flush=True)
            elif kind == "ready":
                print(ink.ok("[OK] READY"), flush=True)
            elif kind == "command":
                print(ink.white(f"» {text}"), flush=True)
            elif kind == "reply":
                print(text, flush=True)
            elif kind == "warn":
                print(ink.white(f"[WARN] {text}"), flush=True)

        listener = Listener(
            options,
            transcribe=transcriber,
            ask=Conversation(client).ask,
            speak=speaker.say,
            chime=chime,
            log=log,
        )
        return await listener.run(microphone(command))
    finally:
        await speaker.aclose()
        client.close()


def _meter_line(ink: Ink) -> Any:
    tty = ink.on

    def show(vad: VAD, seconds: float | None) -> None:
        if seconds is not None:
            print(("\r\033[K" if tty else "") + ink.ok(f"[OK] UTTERANCE {seconds:4.1f}S"))
            return
        db = 20 * math.log10(max(vad.level, 1.0) / 32768)
        filled = round((max(-60.0, db) + 60) / 2)
        bar = "#" * filled + "-" * (30 - filled)
        state = "SPEECH" if vad.speaking else "------"
        line = (
            f"LVL {bar} {vad.level:05.0f}  FLOOR {vad.floor or 0:05.0f}  "
            f"THR {vad.threshold:05.0f}  {state}"
        )
        print(("\r" + line) if tty else line, end="" if tty else "\n", flush=True)

    return show


def register(sub: argparse._SubParsersAction) -> None:
    speak = sub.add_parser("speak", help="Say something in Bagley's voice")
    speak.add_argument("text", nargs="*", help="What to say (Markdown is fine); or pipe it in")
    speak.add_argument("-o", "--out", help="Write the audio to this file instead of playing it")
    speak.add_argument("--voice", default="", help="Piper voice for local speech (path or name)")
    speak.add_argument("--local", action="store_true", help="Don't use the running server")
    speak.set_defaults(func=cmd_speak)

    transcribe = sub.add_parser("transcribe", help="Transcribe an audio file with whisper.cpp")
    transcribe.add_argument("file", help="WAV, WebM, Ogg, MP4, FLAC or MP3")
    transcribe.add_argument("--model", default="", help="ggml model path or name, e.g. base.en")
    transcribe.add_argument("-l", "--language", default="auto", help="auto or a code like en")
    transcribe.add_argument("--local", action="store_true", help="Don't use the running server")
    transcribe.set_defaults(func=cmd_transcribe)

    listen = sub.add_parser("listen", help="Listen for the wake word and answer out loud")
    listen.add_argument("--once", action="store_true", help="Exit after one command")
    listen.add_argument("--wake-word", default="", help="Wake word (default: preferences)")
    listen.add_argument(
        "--no-wake", action="store_true", help="Every utterance is a command (for key bindings)"
    )
    listen.add_argument("--device", default="", help="Recording device or PipeWire target")
    listen.add_argument("--model", default="", help="ggml model path or name, e.g. base.en")
    listen.add_argument("--voice", default="", help="Piper voice for local speech (path or name)")
    listen.add_argument("-v", "--verbose", action="store_true", help="Show everything heard")
    listen.add_argument(
        "--test-mic", action="store_true", help="Show microphone levels and detected speech"
    )
    listen.set_defaults(func=cmd_listen)

    voices = sub.add_parser("voices", help="Piper voices found and voice engine status")
    voices.add_argument("--json", action="store_true", help="Print the status as JSON")
    voices.set_defaults(func=cmd_voices)
