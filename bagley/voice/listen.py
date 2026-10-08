"""``bagley listen``: the desktop voice loop.

The microphone streams 16 kHz mono PCM from ``pw-record`` (or ``parecord``, ``arecord``) into
the VAD. Each utterance is transcribed with whisper.cpp, here or on the server. An utterance
that starts with the wake word is a command ("Bagley, what's on today?"); the wake word on its
own plays a ready tone and the next utterance is the command. The reply is spoken with the
server's voice (Piper or ElevenLabs), else local Piper, else printed. Listening pauses while
Bagley thinks and speaks, so he never answers himself.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from bagley.voice import VoiceError, VoiceUnavailable, find_program, last_line
from bagley.voice.audio import SPEECH_RATE, play, tone, wav_bytes
from bagley.voice.stt import resolve_model, transcribe_audio, whisper_binary
from bagley.voice.text import chunk, limit_text, speakable
from bagley.voice.tts import MAX_CHARS, elevenlabs, piper, piper_binary, prepare, resolve_voice
from bagley.voice.wake import VAD, detect

if TYPE_CHECKING:
    from bagley.client import Client
    from bagley.config import Preferences

ARMED_SECONDS = 8.0  # After the wake word alone, how long the next utterance counts as a command.
FOLLOW_UP_SECONDS = 300.0  # Commands within this time continue the same conversation.
DEAF_SECONDS = 0.4  # Ignore the microphone this long after Bagley stops speaking.
READ_BYTES = SPEECH_RATE * 2 // 10  # 100 ms of audio per read.

# Recorders that write raw 16 kHz mono s16le to stdout, and how each picks a device.
RECORDERS: list[tuple[str, list[str], Callable[[str], list[str]]]] = [
    (
        "pw-record",
        ["--rate", "16000", "--channels", "1", "--format", "s16"],
        lambda d: ["--target", d],
    ),
    (
        "parecord",
        ["--raw", "--rate=16000", "--channels=1", "--format=s16le"],
        lambda d: [f"--device={d}"],
    ),
    ("arecord", ["-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"], lambda d: ["-D", d]),
]


def recorder_command(device: str = "") -> list[str] | None:
    """The first recorder found, with its arguments."""
    for name, args, pick in RECORDERS:
        path = find_program(name)
        if path:
            command = [path, *args, *(pick(device) if device else [])]
            return [*command, "-"] if name == "pw-record" else command
    return None


async def microphone(command: list[str]) -> AsyncIterator[bytes]:
    """PCM from a recorder process until it exits; the process stops with the iterator."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise VoiceUnavailable(f"Could not start {Path(command[0]).name}: {exc}") from exc
    assert proc.stdout is not None and proc.stderr is not None
    errors: deque[bytes] = deque(maxlen=20)

    async def drain(stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            errors.append(line)

    drainer = asyncio.create_task(drain(proc.stderr))
    try:
        while data := await proc.stdout.read(READ_BYTES):
            yield data
        await proc.wait()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(drainer), timeout=1)
        raise VoiceError(
            f"{Path(command[0]).name} stopped ({proc.returncode}): {last_line(b''.join(errors))}"
        )
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        drainer.cancel()
        await asyncio.gather(drainer, return_exceptions=True)


@dataclass
class Options:
    wake_word: str = "bagley"
    once: bool = False  # Exit after one command.
    no_wake: bool = False  # Every utterance is a command (push-to-talk from a key binding).
    verbose: bool = False


class Listener:
    """The loop: utterances in, commands out. Every step is a callable so tests can fake it.

    ``transcribe`` takes 16 kHz PCM and returns text, ``ask`` takes a command and returns the
    reply, ``speak`` says it and ``chime`` plays the ready tone."""

    def __init__(
        self,
        options: Options,
        *,
        transcribe: Callable[[bytes], Awaitable[str]],
        ask: Callable[[str], Awaitable[str]],
        speak: Callable[[str], Awaitable[Any]],
        chime: Callable[[], Awaitable[Any]],
        log: Callable[[str, str], None] = lambda kind, text: None,
        clock: Callable[[], float] = time.monotonic,
        vad: VAD | None = None,
    ) -> None:
        self.options = options
        self.transcribe = transcribe
        self.ask = ask
        self.speak = speak
        self.chime = chime
        self.log = log  # (kind, text): kind is "heard", "ready", "command", "reply" or "warn".
        self.clock = clock
        self.vad = vad or VAD()
        self.armed_until = 0.0
        self.deaf_until = 0.0
        self.busy = False

    async def run(self, source: AsyncIterator[bytes]) -> int:
        """Listen until the source ends, or after one command with ``once``."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        failure: list[BaseException] = []

        async def pump() -> None:
            # Keep reading while busy so the recorder never backs up, but drop what it hears:
            # that is Bagley's own voice.
            try:
                async for data in source:
                    if not self.busy and self.clock() >= self.deaf_until:
                        queue.put_nowait(data)
            except VoiceError as exc:
                failure.append(exc)
            finally:
                queue.put_nowait(None)

        reader = asyncio.create_task(pump())
        try:
            while (data := await queue.get()) is not None:
                for utterance in self.vad.feed(data):
                    self.busy = True
                    try:
                        ran = await self.handle(utterance)
                    finally:
                        while not queue.empty():
                            if queue.get_nowait() is None:
                                queue.put_nowait(None)
                                break
                        self.vad.reset()
                        self.deaf_until = self.clock() + DEAF_SECONDS
                        self.busy = False
                    if ran and self.options.once:
                        return 0
                    break  # Anything after it in this read was heard while busy.
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if failure:
            raise failure[0]
        return 0

    async def handle(self, pcm: bytes) -> bool:
        """One utterance. Returns True when it ran a command."""
        try:
            text = await self.transcribe(pcm)
        except VoiceError as exc:
            self.log("warn", str(exc))
            return False
        if not text:
            return False
        armed = self.armed_until > self.clock()
        self.armed_until = 0.0
        command = detect(text, self.options.wake_word)
        if command is None:
            if not (armed or self.options.no_wake):
                self.log("heard", text)
                return False
            command = text
        if not command:
            self.log("ready", text)
            with contextlib.suppress(VoiceError):
                await self.chime()
            self.armed_until = self.clock() + ARMED_SECONDS
            return False
        self.log("command", command)
        try:
            reply = await self.ask(command)
        except VoiceError as exc:
            self.log("warn", str(exc))
            return True
        if reply:
            self.log("reply", reply)
            try:
                await self.speak(reply)
            except VoiceError as exc:
                self.log("warn", str(exc))
        return True


# The real pieces --------------------------------------------------------------------------------


class Conversation:
    """Sends commands to Bagley (the server, or this process when none runs) and keeps
    follow-ups in the same chat for a few minutes."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.conversation_id: str | None = None
        self.last = 0.0

    def _ask(self, text: str) -> str:
        from bagley.client import ask, reply_text

        body: dict[str, Any] = {"text": text, "source": "voice", "approvals": "ask"}
        if self.conversation_id and time.monotonic() - self.last < FOLLOW_UP_SECONDS:
            body["conversation_id"] = self.conversation_id

        def tap(events: Any) -> Any:
            for event in events:
                if event.get("type") == "run.start" and event.get("conversation_id"):
                    self.conversation_id = event["conversation_id"]
                yield event

        reply, errors = reply_text(tap(ask(body, client=self.client)))
        self.last = time.monotonic()
        if errors and not reply:
            raise VoiceError(str(errors[0].get("message") or "The model failed."))
        return reply

    async def ask(self, text: str) -> str:
        from bagley.client import ServerUnavailable

        try:
            return await asyncio.to_thread(self._ask, text)
        except ServerUnavailable as exc:
            raise VoiceError(f"Bagley is not reachable: {exc}") from exc


class Speaker:
    """Speaks replies with the server's engine when it has one, else local Piper (or the local
    preferences' ElevenLabs when no server runs). Tells the server while it speaks, so the
    avatar shows VOICE. Synthesizes the next sentences while the current ones play."""

    def __init__(
        self,
        client: Client | None,
        prefs: Preferences,
        data_dir: Path,
        *,
        voice: str = "",
        player: Callable[[bytes, str], Awaitable[None]] = play,
    ) -> None:
        self.client = client
        self.prefs = prefs
        self.data_dir = data_dir
        self.voice = voice
        self.player = player
        self.mode = ""
        self._http: httpx.AsyncClient | None = None

    async def plan(self) -> str:
        """Where speech comes from: "server", "piper", "elevenlabs", or "" (print only)."""
        from bagley.client import ServerUnavailable

        reachable = False
        if self.client is not None:
            try:
                status = await asyncio.to_thread(self.client.get, "/api/voice/status")
                reachable = True
                if status.get("tts_ready"):
                    return "server"
            except ServerUnavailable:
                pass
        if not reachable and self.prefs.tts_engine == "elevenlabs" and self.prefs.elevenlabs_key:
            return "elevenlabs"
        if piper_binary() and resolve_voice(self.voice or self.prefs.piper_voice, self.data_dir):
            return "piper"
        return ""

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        if self.mode == "server":
            assert self.client is not None
            resp = await _post(self.client, "/api/tts", "Server voice", json={"text": text})
            return resp.content, resp.headers.get("content-type", "audio/wav")
        spoken = prepare(text, character=self.prefs.persona == "bagley")
        if self.mode == "elevenlabs":
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=60)
            audio = await elevenlabs(
                self._http, self.prefs.elevenlabs_key, self.prefs.elevenlabs_voice, spoken
            )
            return audio, "audio/mpeg"
        voice = resolve_voice(self.voice or self.prefs.piper_voice, self.data_dir)
        if voice is None:
            raise VoiceUnavailable("No Piper voice found.")
        return await piper(spoken, voice), "audio/wav"

    async def say(self, text: str) -> bool:
        """Speak ``text`` (Markdown is fine). False when no engine is available."""
        self.mode = await self.plan()
        if not self.mode:
            return False
        pieces = chunk(limit_text(speakable(text), MAX_CHARS))
        if not pieces:
            return True
        await self._speaking(True, len(" ".join(pieces)) / 12 + 15)
        try:
            pending = asyncio.create_task(self.synthesize(pieces[0]))
            for i in range(len(pieces)):
                audio, mime = await pending
                if i + 1 < len(pieces):
                    pending = asyncio.create_task(self.synthesize(pieces[i + 1]))
                await self.player(audio, mime)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await self._speaking(False)
        return True

    async def _speaking(self, speaking: bool, seconds: float = 120) -> None:
        if self.client is None:
            return
        from bagley.client import ServerUnavailable

        with contextlib.suppress(ServerUnavailable):
            await asyncio.to_thread(
                self.client.post,
                "/api/voice/speaking",
                {"speaking": speaking, "seconds": min(900, max(1, seconds))},
            )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()


class Transcriber:
    """Transcribes utterances with whisper.cpp on this computer, or on the server when this one
    has no whisper.cpp or model."""

    def __init__(self, client: Client | None, prefs: Preferences, data_dir: Path, model: str = ""):
        self.client = client
        self.binary = whisper_binary()
        self.model = resolve_model(model or prefs.whisper_model, data_dir)
        self.language = "auto"
        self.mode = ""

    async def plan(self) -> str:
        """Where transcription runs: "local", "server", or "" when nowhere."""
        from bagley.client import ServerUnavailable

        if self.binary and self.model:
            self.mode = "local"
        elif self.client is not None:
            with contextlib.suppress(ServerUnavailable):
                status = await asyncio.to_thread(self.client.get, "/api/voice/status")
                if status.get("stt_ready"):
                    self.mode = "server"
        return self.mode

    async def __call__(self, pcm: bytes) -> str:
        """Transcribe one utterance of 16 kHz PCM."""
        return await self.audio(wav_bytes(pcm, SPEECH_RATE), "audio/wav")

    async def audio(self, data: bytes, mime: str) -> str:
        """Transcribe a recording in any format ffmpeg reads."""
        if self.mode == "local":
            assert self.model is not None
            return await transcribe_audio(
                data, mime, model=self.model, binary=self.binary, language=self.language
            )
        if self.mode != "server" or self.client is None:
            raise VoiceUnavailable("No speech recognition: install whisper.cpp and a model.")
        resp = await _post(
            self.client,
            "/api/stt",
            "Server transcription",
            content=data,
            headers={"Content-Type": mime},
        )
        try:
            return str(resp.json().get("text", ""))
        except (ValueError, AttributeError) as exc:
            raise VoiceError("Server transcription returned no text.") from exc


async def _post(client: Client, path: str, what: str, **kwargs: Any) -> httpx.Response:
    """POST to the server; any failure becomes a ``VoiceError`` the loop can report."""
    try:
        resp = await asyncio.to_thread(client.http.post, path, timeout=150, **kwargs)
    except httpx.HTTPError as exc:
        raise VoiceError(f"Bagley is not reachable: {exc}") from exc
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except (ValueError, AttributeError):
            detail = resp.text[:200]
        raise VoiceError(f"{what} failed ({resp.status_code}): {detail}")
    return resp


async def chime() -> None:
    await play(tone(), "audio/wav")


async def meter(
    source: AsyncIterator[bytes],
    show: Callable[[VAD, float | None], None],
    *,
    vad: VAD | None = None,
    every: float = 0.1,
) -> None:
    """``bagley listen --test-mic``: calls ``show(vad, None)`` about every ``every`` seconds of
    audio with the current level, and ``show(vad, seconds)`` for each utterance found."""
    vad = vad or VAD()
    heard = 0
    step = int(SPEECH_RATE * 2 * every)
    async for data in source:
        for utterance in vad.feed(data):
            show(vad, len(utterance) / (SPEECH_RATE * 2))
        heard += len(data)
        if heard >= step:
            heard = 0
            show(vad, None)
