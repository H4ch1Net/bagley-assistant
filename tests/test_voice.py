from __future__ import annotations

import asyncio
import io
import json
import math
import random
import re
import shutil
import struct
import sys
import time
from array import array
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bagley import cli
from bagley.client import Client
from bagley.config import Preferences
from bagley.server import create_app
from bagley.voice import VoiceError, VoiceUnavailable, audio, listen, stt, text, tts, wake

pytestmark = pytest.mark.anyio

RATE = 16_000
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64  # Enough of a WebM header to be recognised.


class FakeRunner:
    """Stands in for ``run_program``: records calls and answers from a script."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], bytes | None, float]] = []

    async def __call__(self, args, stdin, timeout):
        self.calls.append((list(args), stdin, timeout))
        result = self.results.pop(0)
        return result(args, stdin) if callable(result) else result


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No real voices, models or programs from the computer running the tests."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setattr(tts, "SYSTEM_VOICES", [])
    monkeypatch.setattr(stt, "SYSTEM_SHARE", [])


@pytest.fixture
def programs(monkeypatch):
    """Programs that ``shutil.which`` finds; add names to the returned set."""
    found: set[str] = set()

    def which(name, *args, **kwargs):
        return f"/nonexistent/bin/{name}" if name in found else None

    monkeypatch.setattr(shutil, "which", which)
    return found


def make_voice(folder: Path, name: str = "en_GB-alan-medium", rate: int = 16_000) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    voice = folder / f"{name}.onnx"
    voice.write_bytes(b"onnx")
    config = {"audio": {"sample_rate": rate, "quality": "medium"}, "language": {"code": "en_GB"}}
    Path(f"{voice}.json").write_text(json.dumps(config))
    return voice


def make_model(folder: Path, name: str = "ggml-base.en.bin") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    model = folder / name
    model.write_bytes(b"ggml")
    return model


def pcm(values) -> bytes:
    return array("h", values).tobytes()


def silence(seconds: float) -> bytes:
    return pcm([0] * int(RATE * seconds))


def sine(seconds: float, amp: int = 8000, freq: float = 220.0) -> bytes:
    return pcm(
        int(amp * math.sin(2 * math.pi * freq * i / RATE)) for i in range(int(RATE * seconds))
    )


def noise(seconds: float, amp: int, seed: int = 1) -> bytes:
    rng = random.Random(seed)
    return pcm(int(rng.uniform(-amp, amp)) for _ in range(int(RATE * seconds)))


def streamed_wav(data: bytes, rate: int = RATE) -> bytes:
    """A WAV as ffmpeg writes it to a pipe: sizes unknown (0xFFFFFFFF)."""
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF\xff\xff\xff\xffWAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data\xff\xff\xff\xff"
        + data
    )


def utterances(vad: wake.VAD, data: bytes) -> list[float]:
    found = []
    for i in range(0, len(data), 4000):
        found += vad.feed(data[i : i + 4000])
    return [len(u) / (RATE * 2) for u in found]


# Speakable text ---------------------------------------------------------------------------------

REPLY = """## Disk report

Your **home** folder is *mostly* fine, see [the guide](https://example.com/guide) or https://www.github.com/x/y.

```bash
du -sh ~/*
```

| Folder | Size |
|--------|-----:|
| dev    | 12G  |

- `node_modules` takes 4G 🙂
- Clear the cache -> 2G back

> Disk space & patience are finite
"""


def test_speakable_markdown():
    assert text.speakable(REPLY) == (
        "Disk report. Your home folder is mostly fine, see the guide or github.com. "
        "(code omitted). Table: Folder, Size. node modules takes 4G. "
        "Clear the cache to 2G back. Disk space and patience are finite."
    )
    assert text.speakable("```py\nprint(1)\n```\n\n```py\nprint(2)\n```") == "(code omitted)."
    assert text.speakable("Unclosed\n```\nrm -rf /") == "Unclosed. (code omitted)."
    assert text.speakable("![chart](x.png)\n\n---\n\n[1]: https://a.b") == "chart."
    assert text.speakable("Run `ls` then `" + "x" * 50 + "`.") == "Run ls then (code omitted)."
    assert text.speakable("snake_case and/or 1/2 ~~old~~ new") == "snake case and or 1/2 old new."
    assert text.speakable("   \n\n  ") == ""


def test_chunk_keeps_sentences_whole():
    sentences = [f"Sentence number {i} is here, Dr. Who said e.g. this." for i in range(40)]
    source = " ".join(sentences)
    pieces = text.chunk(source)
    assert all(len(p) <= text.CHUNK_CHARS for p in pieces) and len(pieces) > 3
    assert " ".join(pieces) == source
    assert all(p.endswith("this.") for p in pieces)  # Never split after "Dr." or "e.g.".

    endless = "word " * 300
    pieces = text.chunk(endless, 100)
    assert all(len(p) <= 100 for p in pieces) and " ".join(pieces) == endless.strip()
    assert text.chunk("") == []


def test_limit_text_cuts_at_a_sentence():
    source = " ".join(f"This is sentence {i}." for i in range(400))
    cut = text.limit_text(source, 2500)
    assert len(cut) <= 2500 and cut.endswith(".") and source.startswith(cut)
    assert text.limit_text("Short.", 2500) == "Short."


def test_in_character_only_changes_punctuation():
    source = "Brilliant! Naturally I did it — again (as usual). Really?! Of course not… Well done"
    spoken = text.in_character(source)
    assert spoken == (
        "Brilliant. Naturally, I did it, again, as usual. Really? Of course not... Well done"
    )
    assert re.findall(r"\w+", spoken) == re.findall(r"\w+", source)
    assert text.in_character("Of course it works.") == "Of course, it works."
    assert text.in_character("Well-known, obviously wrong.") == "Well-known, obviously wrong."


# Piper ------------------------------------------------------------------------------------------


async def test_piper_arguments_and_wav_wrapping(tmp_path, programs):
    programs.add("piper-tts")
    voice = make_voice(tmp_path / "voices", rate=16_000)
    raw = pcm([0, 1000, -1000, 0] * 100)
    runner = FakeRunner((0, raw, b""))
    wav = await tts.piper("Hello there.\nSecond line.", voice, runner=runner)
    args, stdin, timeout = runner.calls[0]
    assert args == [
        "/nonexistent/bin/piper-tts",
        "--model",
        str(voice),
        "--output_raw",
        "--length_scale",
        "0.95",
        "--sentence_silence",
        "0.25",
    ]
    assert stdin == b"Hello there. Second line.\n" and timeout > 0
    assert audio.read_wav(wav) == (16_000, 1, 2, raw)

    already = audio.wav_bytes(raw, 22_050)  # A Piper that writes WAV anyway.
    assert await tts.piper("Hi.", voice, runner=FakeRunner((0, already, b""))) == already

    with pytest.raises(VoiceError, match="Piper failed: bad model"):
        await tts.piper("Hi.", voice, runner=FakeRunner((1, b"", b"loading\nbad model\n")))
    with pytest.raises(VoiceError, match="no audio"):
        await tts.piper("Hi.", voice, runner=FakeRunner((0, b"", b"")))
    programs.clear()
    with pytest.raises(VoiceUnavailable, match="not installed"):
        await tts.piper("Hi.", voice, runner=FakeRunner())


def test_piper_binary_skips_the_mouse_configurator(tmp_path, monkeypatch):
    mouse = tmp_path / "piper"
    mouse.write_text("#!/usr/bin/python3\nimport gi\ngi.require_version('Gtk', '3.0')\n")
    paths = {"piper": str(mouse)}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: paths.get(name))
    assert tts.piper_binary() is None
    paths["piper-tts"] = "/nonexistent/bin/piper-tts"
    assert tts.piper_binary() == "/nonexistent/bin/piper-tts"


def test_find_and_resolve_voices(tmp_path):
    data = tmp_path / "data"
    other = make_voice(tmp_path / "share" / "piper" / "en_US", "en_US-ryan-high", 22_050)
    alan = make_voice(data / "voices", "en_GB-alan-medium")
    northern = make_voice(data / "voices", "en_GB-northern_english_male-medium")
    voices = tts.find_voices(data)
    assert [v["name"] for v in voices] == [alan.stem, northern.stem, other.stem]
    assert voices[0] == {
        "name": "en_GB-alan-medium",
        "path": str(alan),
        "language": "en_GB",
        "quality": "medium",
        "sample_rate": 16_000,
        "config": True,
    }
    assert tts.resolve_voice("", data) == alan
    assert tts.resolve_voice("northern_english_male", data) == northern
    assert tts.resolve_voice("en_US-ryan-high", data) == other
    assert tts.resolve_voice(str(other), data) == other
    assert tts.resolve_voice(str(tmp_path / "missing.onnx"), data) is None
    assert tts.resolve_voice("nobody", data) is None
    assert tts.sample_rate(tmp_path / "unknown.onnx") == tts.DEFAULT_SAMPLE_RATE


async def test_synthesize_with_piper_and_cache(make_runtime, programs, monkeypatch):
    rt = make_runtime(tts_engine="piper")
    with pytest.raises(VoiceUnavailable, match="No Piper voice"):
        await tts.synthesize(rt, "Hello")
    voice = make_voice(rt.config.data_dir / "voices")
    programs.add("piper-tts")
    runner = FakeRunner((0, pcm([5] * 10), b""), (0, pcm([6] * 10), b""))
    first = await tts.synthesize(rt, "**Done!** See `ls`.", runner=runner)
    again = await tts.synthesize(rt, "**Done!** See `ls`.", runner=runner)
    assert first == again and first[1] == "audio/wav" and len(runner.calls) == 1
    assert runner.calls[0][0][2] == str(voice)
    assert runner.calls[0][1] == b"Done. See ls.\n"  # In character: no exclamations.

    rt.update_preferences({"persona": "professional"})
    await tts.synthesize(rt, "**Done!**", runner=runner)
    assert runner.calls[1][1] == b"Done!\n"

    rt.update_preferences({"tts_engine": "browser"})
    with pytest.raises(VoiceUnavailable, match="browser"):
        await tts.synthesize(rt, "Hello")


def test_speech_cache_is_bounded():
    cache = tts.SpeechCache(size=2)
    for key in "abc":
        cache.put(key, (key.encode(), "audio/wav"))
    assert cache.get("a") is None and cache.get("c") == (b"c", "audio/wav")


# ElevenLabs -------------------------------------------------------------------------------------


async def elevenlabs_runtime(make_runtime, handler, **prefs):
    rt = make_runtime(tts_engine="elevenlabs", elevenlabs_key="sk-secret", **prefs)
    await rt.http.aclose()
    rt.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return rt


async def test_elevenlabs_request_and_cache(make_runtime):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ID3mp3", headers={"content-type": "audio/mpeg"})

    rt = await elevenlabs_runtime(make_runtime, handler)
    result = await tts.synthesize(rt, "**Hello!** Naturally I agree.")
    assert result == (b"ID3mp3", "audio/mpeg")
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://api.elevenlabs.io/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb"
        "?output_format=mp3_44100_128"
    )
    assert request.headers["xi-api-key"] == "sk-secret"
    assert json.loads(request.content) == {
        "text": "Hello. Naturally, I agree.",
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.2},
    }
    await tts.synthesize(rt, "**Hello!** Naturally I agree.")
    assert len(seen) == 1  # Served from the cache.

    rt.update_preferences({"elevenlabs_voice": "onwK4e9ZLuTAKqWW03F9"})
    await tts.synthesize(rt, "**Hello!** Naturally I agree.")
    assert "/text-to-speech/onwK4e9ZLuTAKqWW03F9" in str(seen[1].url)


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (401, {"detail": {"status": "invalid_api_key", "message": "Bad sk-secret"}}, "API key"),
        (401, {"detail": {"status": "quota_exceeded", "message": "0 credits left"}}, "quota"),
        (429, {"detail": {"status": "too_many_concurrent_requests"}}, "rate limiting"),
        (404, {"detail": {"status": "voice_not_found"}}, "no voice"),
        (500, {"detail": "Server melted, key sk-secret"}, "answered 500"),
    ],
)
async def test_elevenlabs_errors(make_runtime, status, body, message):
    rt = await elevenlabs_runtime(make_runtime, lambda r: httpx.Response(status, json=body))
    with pytest.raises(VoiceError, match=message) as caught:
        await tts.synthesize(rt, "Hello")
    assert "sk-secret" not in str(caught.value)


async def test_elevenlabs_setup_errors(make_runtime):
    rt = await elevenlabs_runtime(
        make_runtime, lambda r: httpx.Response(200, json={}), elevenlabs_voice="../../v1/user"
    )
    with pytest.raises(VoiceUnavailable, match="letters and digits"):
        await tts.synthesize(rt, "Hello")
    rt.update_preferences({"elevenlabs_voice": "", "elevenlabs_key": ""})
    with pytest.raises(VoiceUnavailable, match="API key"):
        await tts.synthesize(rt, "Hello")
    rt.update_preferences({"elevenlabs_key": "sk-secret"})
    with pytest.raises(VoiceError, match="no audio"):
        await tts.synthesize(rt, "Hello")  # JSON instead of audio.


# Speech to text ---------------------------------------------------------------------------------


def test_parse_whisper_output():
    raw = (
        "[00:00:00.000 --> 00:00:02.000]   Bagley, what's the time?\n"
        "[BLANK_AUDIO]\n"
        " (wind blowing) And the weather. *coughs*\n"
        "whisper_print_timings: total time = 812.00 ms\n"
    )
    assert stt.parse_output(raw) == "Bagley, what's the time? And the weather."
    assert stt.parse_output("[BLANK_AUDIO]\n[ Silence ]\n♪♪\n") == ""


async def test_transcribe_converts_with_ffmpeg(make_runtime, programs, tmp_path):
    programs.update({"ffmpeg", "whisper-cli"})
    model = make_model(tmp_path / "models")
    rt = make_runtime(whisper_model=str(model))
    speech = sine(0.5, 3000)
    seen = {}

    def whisper(args, stdin):
        wav = Path(args[args.index("-f") + 1])
        seen["wav"] = wav
        seen["info"] = audio.read_wav(wav.read_bytes())[:3]
        Path(f"{wav}.txt").write_text(" Bagley, lights off.\n[BLANK_AUDIO]\n")
        return 0, b"Bagley, lights off.\n", b""

    runner = FakeRunner((0, streamed_wav(speech), b""), whisper)
    assert await stt.transcribe(rt, WEBM, "audio/webm", runner=runner) == "Bagley, lights off."
    (ffmpeg, stdin, _), (whisper_args, _, _) = runner.calls
    joined = " ".join(ffmpeg)
    assert ffmpeg[0] == "/nonexistent/bin/ffmpeg" and stdin == WEBM
    assert (
        "-i pipe:0" in joined and "-ar 16000 -ac 1" in joined and joined.endswith("-f wav pipe:1")
    )
    assert "-protocol_whitelist pipe -f matroska -i pipe:0" in joined  # Only the format sent.
    assert whisper_args[:2] == ["/nonexistent/bin/whisper-cli", "-m"]
    assert whisper_args[2:] == [
        str(model),
        "-f",
        str(seen["wav"]),
        "-nt",
        "-np",
        "-l",
        "auto",
        "-otxt",
    ]
    assert seen["info"] == (16_000, 1, 2)
    assert not seen["wav"].parent.exists()  # Temporary files are gone.


async def test_transcribe_skips_conversion_for_speech_wav(programs, tmp_path):
    programs.add("whisper-cli")  # No ffmpeg on PATH.
    model = make_model(tmp_path)
    runner = FakeRunner((0, b"[00:00:00.000 --> 00:00:01.000]  Hello.\n", b""))
    wav = audio.wav_bytes(sine(0.3), RATE)
    assert await stt.transcribe_audio(wav, "audio/wav", model=model, runner=runner) == "Hello."
    assert len(runner.calls) == 1


async def test_transcribe_errors(make_runtime, programs, tmp_path):
    model = make_model(tmp_path)
    rt = make_runtime(whisper_model=str(model))
    with pytest.raises(VoiceUnavailable, match=r"whisper\.cpp is not installed"):
        await stt.transcribe(rt, WEBM, "audio/webm", runner=FakeRunner())
    programs.add("whisper-cli")
    with pytest.raises(VoiceUnavailable, match="ffmpeg is needed"):
        await stt.transcribe(rt, WEBM, "audio/webm", runner=FakeRunner())
    with pytest.raises(VoiceError, match="Unsupported audio"):
        await stt.transcribe(rt, b"#EXTM3U\nfile:///etc/passwd\n", "audio/x-mpegurl")
    programs.add("ffmpeg")
    with pytest.raises(VoiceError, match="ffmpeg could not read"):
        await stt.transcribe(rt, WEBM, "audio/webm", runner=FakeRunner((1, b"", b"EBML error")))
    wav = audio.wav_bytes(sine(0.2), RATE)
    with pytest.raises(VoiceError, match=r"whisper\.cpp failed: out of memory"):
        await stt.transcribe(rt, wav, "audio/wav", runner=FakeRunner((3, b"", b"out of memory")))
    rt.update_preferences({"whisper_model": str(tmp_path / "missing.bin")})
    with pytest.raises(VoiceUnavailable, match="model not found"):
        await stt.transcribe(rt, wav, "audio/wav", runner=FakeRunner())


def test_whisper_binary_and_models(programs, tmp_path):
    programs.update({"main", "whisper"})
    assert stt.whisper_binary() == "/nonexistent/bin/whisper"
    programs.add("whisper-cli")
    assert stt.whisper_binary() == "/nonexistent/bin/whisper-cli"

    data = tmp_path / "data"
    small = make_model(data / "models", "ggml-small.bin")
    base = make_model(tmp_path / "share" / "whisper", "ggml-base.en.bin")
    make_model(data / "models", "ggml-silero-v5.1.2.bin")  # A VAD model, not for speech.
    assert stt.find_models(data) == [base, small]
    assert stt.resolve_model("", data) == base
    assert stt.resolve_model("small", data) == small
    assert stt.resolve_model(str(small), data) == small
    assert stt.resolve_model("large", data) is None


# Audio ------------------------------------------------------------------------------------------


def test_wav_helpers_and_tone():
    beep = audio.tone()
    rate, channels, width, data = audio.read_wav(beep)
    assert (rate, channels, width) == (22_050, 1, 2)
    assert abs(audio.duration(beep) - 0.12) < 0.001
    values = audio.samples(data)
    assert values[0] == 0 and max(values) > 9000  # Fades in, then rings at 30% volume.

    speech = sine(0.25)
    assert audio.read_wav(streamed_wav(speech)) == (RATE, 1, 2, speech)
    assert audio.is_speech_wav(audio.wav_bytes(speech, RATE))
    assert not audio.is_speech_wav(audio.wav_bytes(speech, 44_100))
    assert audio.read_wav(b"RIFF....WAVEjunk") is None
    assert [audio.sniff(x) for x in (WEBM, b"OggS..", b"ID3...", b"\0\0\0\x18ftypM4A ")] == [
        "matroska",
        "ogg",
        "mp3",
        "mov",
    ]


async def test_play_uses_the_best_player(programs):
    programs.update({"aplay", "ffplay"})
    seen = []

    def player(args, stdin):
        seen.append((args, Path(args[-1]).read_bytes()))
        return 0, b"", b""

    await audio.play(b"RIFFwav", "audio/wav", runner=FakeRunner(player))
    await audio.play(b"ID3mp3", "audio/mpeg", runner=FakeRunner(player))
    assert seen[0][0][:2] == ["/nonexistent/bin/aplay", "-q"] and seen[0][1] == b"RIFFwav"
    assert seen[1][0][0] == "/nonexistent/bin/ffplay" and seen[1][0][-1].endswith(".mp3")
    assert not Path(seen[0][0][-1]).exists()
    assert audio.players("audio/wav") == ["aplay", "ffplay"]
    with pytest.raises(VoiceError, match="aplay failed"):
        await audio.play(b"RIFF", "audio/wav", runner=FakeRunner((1, b"", b"no device")))
    programs.clear()
    with pytest.raises(VoiceUnavailable, match="No audio player"):
        await audio.play(b"RIFF", "audio/wav", runner=FakeRunner())


# Voice activity ---------------------------------------------------------------------------------


def test_vad_finds_speech_and_ignores_silence_and_noise():
    assert utterances(wake.VAD(), silence(5)) == []
    assert utterances(wake.VAD(), noise(5, 400)) == []  # Steady room noise.
    assert utterances(wake.VAD(), silence(1) + sine(0.1) + silence(1)) == []  # A click.

    found = utterances(wake.VAD(), silence(1) + sine(1.0) + silence(2))
    assert len(found) == 1 and 1.1 < found[0] < 1.6  # Pre-roll plus a little tail.

    found = utterances(wake.VAD(), noise(2, 400) + sine(1.2) + noise(2, 400, seed=2))
    assert len(found) == 1

    found = utterances(wake.VAD(), silence(1) + sine(0.8) + silence(1.5) + sine(0.5) + silence(1))
    assert len(found) == 2

    vad = wake.VAD()  # A pause shorter than 700 ms doesn't end the utterance.
    assert len(utterances(vad, silence(1) + sine(0.5) + silence(0.4) + sine(0.5) + silence(1))) == 1


def test_vad_caps_utterances_and_learns_a_loud_room():
    vad = wake.VAD()
    found = utterances(vad, silence(1) + noise(40, 3000, seed=5))
    assert found == [15.0]  # One capped utterance, then the fan is the new floor.
    assert vad.floor is not None and vad.floor > 1500 and not vad.speaking
    vad.reset()
    assert utterances(vad, sine(1.0, amp=20_000) + noise(1.5, 3000)) == pytest.approx(
        [1.0], abs=0.5
    )


# Wake word --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heard", "command"),
    [
        ("Bagley.", ""),
        ("Hey Bagley!", ""),
        ("Baguely?", ""),
        ("Bagly, what's the time?", "what's the time?"),
        ("Badgley open Firefox", "open Firefox"),
        ("bag lee turn it down", "turn it down"),
        ("OK, Bagley — lights off.", "lights off."),
        ("okay so bagley stop", "stop"),
        ("Bailey, mute", "mute"),  # 0.83 similar: a likely mis-hearing.
        ("Bagley's here", "here"),
    ],
)
def test_wake_word_matches(heard, command):
    assert wake.detect(heard) == command


@pytest.mark.parametrize(
    "heard",
    [
        "Bagel please",  # 0.73: food, not a butler.
        "I want a bagel",
        "the Bagley report is due",  # Not at the start.
        "badly done",
        "baggy jeans",
        "Bradley",
        "bag lady",
        "Hey",
        "",
    ],
)
def test_wake_word_rejects(heard):
    assert wake.detect(heard) is None


def test_custom_wake_word():
    assert wake.detect("Jarvis, lights", "jarvis") == "lights"
    assert wake.detect("Bagley, lights", "jarvis") is None
    assert wake.detect("Hey Jarvis lights", "hey jarvis") == "lights"
    assert wake.detect("hey jarvis", "Hey Jarvis") == ""


# The listener -----------------------------------------------------------------------------------


class Scripted:
    """Fake steps for the listener."""

    def __init__(self, *heard: str) -> None:
        self.heard = list(heard)
        self.asked: list[str] = []
        self.spoken: list[str] = []
        self.chimes = 0
        self.logged: list[tuple[str, str]] = []
        self.now = 1000.0

    async def transcribe(self, data: bytes) -> str:
        return self.heard.pop(0)

    async def ask(self, command: str) -> str:
        self.asked.append(command)
        return f"Done: {command}"

    async def speak(self, reply: str) -> None:
        self.spoken.append(reply)

    async def chime(self) -> None:
        self.chimes += 1

    def listener(self, **options) -> listen.Listener:
        return listen.Listener(
            listen.Options(**options),
            transcribe=self.transcribe,
            ask=self.ask,
            speak=self.speak,
            chime=self.chime,
            log=lambda kind, text: self.logged.append((kind, text)),
            clock=lambda: self.now,
        )


async def test_listener_wake_word_then_command():
    s = Scripted("Bagley.", "what's on today", "Bagley, lights off", "nice weather", "Bagley")
    loop = s.listener()
    assert await loop.handle(b"") is False and s.chimes == 1  # Ready tone, armed.
    assert await loop.handle(b"") is True and s.asked == ["what's on today"]
    assert s.spoken == ["Done: what's on today"]
    assert await loop.handle(b"") is True and s.asked[-1] == "lights off"
    assert await loop.handle(b"") is False and ("heard", "nice weather") in s.logged
    assert await loop.handle(b"") is False and s.chimes == 2
    s.heard.append("too late")
    s.now += listen.ARMED_SECONDS + 1
    assert await loop.handle(b"") is False and s.asked[-1] == "lights off"


async def test_listener_without_wake_word_and_failures():
    s = Scripted("open the pod bay doors", "")
    loop = s.listener(no_wake=True)
    assert await loop.handle(b"") is True and s.asked == ["open the pod bay doors"]
    assert await loop.handle(b"") is False  # Nothing heard.

    async def broken(data: bytes) -> str:
        raise VoiceError("whisper.cpp failed: boom")

    loop.transcribe = broken
    assert await loop.handle(b"") is False and s.logged[-1] == ("warn", "whisper.cpp failed: boom")


async def test_listener_run_once_from_a_microphone():
    s = Scripted("Bagley, what's the time?")

    async def mic():
        data = silence(1) + sine(1.0) + silence(1.5) + sine(1.0) + silence(1.5)
        for i in range(0, len(data), 3200):
            yield data[i : i + 3200]
            await asyncio.sleep(0)

    assert await s.listener(once=True).run(mic()) == 0
    assert s.asked == ["what's the time?"] and s.spoken == ["Done: what's the time?"]


async def test_listener_reports_a_dead_microphone():
    async def mic():
        yield silence(0.1)
        raise VoiceError("pw-record stopped (1): no target node")

    with pytest.raises(VoiceError, match="no target node"):
        await Scripted().listener().run(mic())


def test_recorder_command(programs):
    assert listen.recorder_command() is None
    programs.update({"parecord", "arecord"})
    assert listen.recorder_command("usb-mic") == [
        "/nonexistent/bin/parecord",
        "--raw",
        "--rate=16000",
        "--channels=1",
        "--format=s16le",
        "--device=usb-mic",
    ]
    programs.add("pw-record")
    assert listen.recorder_command() == [
        "/nonexistent/bin/pw-record",
        "--rate",
        "16000",
        "--channels",
        "1",
        "--format",
        "s16",
        "-",
    ]
    assert listen.recorder_command("alsa_input.usb")[-3:] == ["--target", "alsa_input.usb", "-"]


async def test_speaker_uses_the_server_voice(tmp_path):
    requests: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.url.path, body))
        if request.url.path == "/api/voice/status":
            return httpx.Response(200, json={"tts_ready": True})
        if request.url.path == "/api/tts":
            return httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"})
        return httpx.Response(200, json={"speaking": body["speaking"]})

    client = Client(url="http://bagley.test", token="")
    client.http = httpx.Client(base_url=client.url, transport=httpx.MockTransport(handler))
    played = []

    async def player(data: bytes, mime: str) -> None:
        played.append((data, mime))

    speaker = listen.Speaker(client, Preferences(), tmp_path, player=player)
    reply = " ".join(f"Sentence {i} is moderately long, as these things go." for i in range(12))
    assert await speaker.say("## Status\n\n" + reply) is True
    paths = [path for path, _ in requests]
    assert paths[0] == "/api/voice/status" and paths[1] == "/api/voice/speaking"
    assert paths[-1] == "/api/voice/speaking" and paths.count("/api/tts") == len(played) == 2
    assert requests[1][1]["speaking"] is True and requests[-1][1] == {
        "speaking": False,
        "seconds": 120,
    }
    tts_texts = [body["text"] for path, body in requests if path == "/api/tts"]
    assert tts_texts[0].startswith("Status. Sentence 0") and all(len(t) <= 400 for t in tts_texts)


async def test_speaker_falls_back_to_local_piper(tmp_path, programs, monkeypatch):
    prefs = Preferences()
    speaker = listen.Speaker(None, prefs, tmp_path)
    assert await speaker.say("Hello.") is False  # Nothing to speak with: print only.

    programs.add("piper-tts")
    make_voice(tmp_path / "voices")
    runner = FakeRunner((0, pcm([1] * 8), b""))
    monkeypatch.setattr(tts, "run_program", runner)
    played = []

    async def player(data: bytes, mime: str) -> None:
        played.append(mime)

    speaker = listen.Speaker(None, prefs, tmp_path, player=player)
    assert await speaker.say("Right away!") is True
    assert played == ["audio/wav"] and runner.calls[0][1] == b"Right away.\n"


# API --------------------------------------------------------------------------------------------


@pytest.fixture
def api(make_runtime):
    rt = make_runtime()
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        c.runtime = rt
        yield c


def test_voice_status_endpoint(api, programs):
    programs.update({"piper-tts", "ffmpeg", "pw-play", "pw-record"})
    voice = make_voice(api.runtime.config.data_dir / "voices")
    api.runtime.update_preferences({"tts_engine": "piper", "stt_engine": "whisper"})
    info = api.get("/api/voice/status").json()
    assert info["tts_engine"] == "piper" and info["tts_ready"] is True
    assert info["stt_engine"] == "whisper" and info["stt_ready"] is False
    assert info["piper"]["binary"] == "/nonexistent/bin/piper-tts"
    assert info["piper"]["voice"] == str(voice)
    assert [v["name"] for v in info["piper"]["voices"]] == ["en_GB-alan-medium"]
    assert info["piper"]["recommended"][0]["name"] == "en_GB-alan-medium"
    assert info["whisper"] == {"binary": None, "model": None, "models": []}
    assert info["ffmpeg"] == "/nonexistent/bin/ffmpeg"
    assert info["elevenlabs"]["configured"] is False
    assert info["players"] == ["pw-play"] and info["recorders"] == ["pw-record"]
    assert info["wake_word"] == "bagley"
    assert any("whisper.cpp is not installed" in p for p in info["problems"])


def test_tts_endpoint(api, programs, monkeypatch):
    resp = api.post("/api/tts", json={"text": "Hello"})
    assert resp.status_code == 409 and "browser" in resp.json()["detail"]

    api.runtime.update_preferences({"tts_engine": "piper"})
    resp = api.post("/api/tts", json={"text": "Hello"})
    assert resp.status_code == 409 and "Piper" in resp.json()["detail"]

    programs.add("piper-tts")
    make_voice(api.runtime.config.data_dir / "voices")
    monkeypatch.setattr(tts, "run_program", FakeRunner((0, pcm([3] * 16), b"")))
    resp = api.post("/api/tts", json={"text": "Hello"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "audio/wav"
    assert resp.content.startswith(b"RIFF") and resp.headers["cache-control"] == "no-store"

    monkeypatch.setattr(tts, "run_program", FakeRunner((1, b"", b"segfault")))
    resp = api.post("/api/tts", json={"text": "Something new"})
    assert resp.status_code == 502 and "segfault" in resp.json()["detail"]
    assert api.post("/api/tts", json={"text": ""}).status_code == 422


def test_stt_endpoint(api, programs, monkeypatch, tmp_path):
    wav = audio.wav_bytes(sine(0.3), RATE)
    resp = api.post("/api/stt", content=wav, headers={"content-type": "audio/wav"})
    assert resp.status_code == 409 and "whisper.cpp" in resp.json()["detail"]

    programs.add("whisper-cli")
    make_model(api.runtime.config.data_dir / "models")
    monkeypatch.setattr(stt, "run_program", FakeRunner((0, b" Bagley, hello.\n", b"")))
    resp = api.post("/api/stt", content=wav, headers={"content-type": "audio/wav"})
    assert resp.status_code == 200 and resp.json() == {"text": "Bagley, hello."}

    resp = api.post("/api/stt", content=b"hello", headers={"content-type": "text/plain"})
    assert resp.status_code == 415
    resp = api.post("/api/stt", content=b"", headers={"content-type": "audio/webm"})
    assert resp.status_code == 422
    resp = api.post("/api/stt", content=b"garbage", headers={"content-type": "audio/webm"})
    assert resp.status_code == 422 and "Unsupported audio" in resp.json()["detail"]


def test_speaking_endpoint(api):
    assert api.post("/api/voice/speaking", json={"speaking": True}).json() == {"speaking": True}
    snap = api.get("/api/activity").json()
    assert snap["state"] == "speaking" and snap["code"] == "VOICE"
    api.post("/api/voice/speaking", json={"speaking": False})
    assert api.get("/api/activity").json()["state"] == "idle"

    api.post("/api/voice/speaking", json={"speaking": True, "seconds": 0.05})
    deadline = time.time() + 3
    while api.get("/api/activity").json()["state"] != "idle" and time.time() < deadline:
        time.sleep(0.02)
    assert api.get("/api/activity").json()["state"] == "idle"  # A client that went quiet.
    assert api.post("/api/voice/speaking", json={"speaking": "loud"}).status_code == 422


# CLI --------------------------------------------------------------------------------------------


@pytest.fixture
def bagley(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    run.data = tmp_path / "data"
    return run


def test_cli_voices(bagley, programs):
    programs.add("piper-tts")
    make_voice(bagley.data / "voices")
    code, out, _ = bagley("voices")
    assert code == 0 and "VOICE // THIS COMPUTER" in out and "en_GB-alan-medium" in out
    assert "PIPER       /nonexistent/bin/piper-tts" in out and "WHISPER     --N/A--" in out
    code, out, _ = bagley("voices", "--json")
    assert code == 0 and json.loads(out)["piper"]["binary"] == "/nonexistent/bin/piper-tts"


def test_cli_speak_and_transcribe_locally(bagley, programs, monkeypatch, tmp_path):
    code, _, err = bagley("speak", "--local", "Hello")
    assert code == 1 and "[CRIT] No voice" in err

    programs.update({"piper-tts", "whisper-cli"})
    make_voice(bagley.data / "voices")
    make_model(bagley.data / "models")
    monkeypatch.setattr(tts, "run_program", FakeRunner((0, pcm([2] * 32), b"")))
    out_file = tmp_path / "hello.wav"
    code, out, _ = bagley("speak", "--local", "--out", str(out_file), "Hello", "there!")
    assert code == 0 and "[OK] audio/wav" in out
    assert audio.read_wav(out_file.read_bytes())[3] == pcm([2] * 32)

    monkeypatch.setattr(stt, "run_program", FakeRunner((0, b"Hello there.\n", b"")))
    code, out, _ = bagley("transcribe", "--local", str(out_file))
    assert code == 0 and out == "Hello there.\n"
    code, _, err = bagley("transcribe", "--local", str(tmp_path / "missing.wav"))
    assert code == 1 and "no such file" in err


# Real processes (the Python interpreter stands in for the programs) -----------------------------


async def test_run_program_pipes_and_times_out():
    from bagley.voice import run_program

    script = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"
    assert await run_program([sys.executable, "-c", script], b"abc", 30) == (0, b"cba", b"")
    with pytest.raises(VoiceError, match="timed out"):
        await run_program([sys.executable, "-c", "import time; time.sleep(30)"], None, 0.5)
    with pytest.raises(VoiceUnavailable, match="Could not start"):
        await run_program([str(Path(sys.executable).parent / "no-such-program")], None, 5)


async def test_microphone_streams_until_the_recorder_stops():
    script = (
        "import sys; sys.stdout.buffer.write(b'\\0' * 9600); sys.stdout.flush(); "
        "sys.stderr.write('device unplugged\\n'); sys.exit(2)"
    )
    received = bytearray()
    with pytest.raises(VoiceError, match=r"stopped \(2\): device unplugged"):
        async for data in listen.microphone([sys.executable, "-c", script]):
            received += data
    assert len(received) == 9600

    endless = (
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'\\0' * 3200)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.05)\n"
    )
    stream = listen.microphone([sys.executable, "-c", endless])
    assert await stream.__anext__()
    await stream.aclose()  # Stops the recorder.


async def test_server_failures_become_voice_errors(tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = Client(url="http://bagley.test", token="")
    client.http = httpx.Client(base_url=client.url, transport=httpx.MockTransport(down))
    transcriber = listen.Transcriber(client, Preferences(), tmp_path)
    transcriber.mode = "server"
    with pytest.raises(VoiceError, match="not reachable"):
        await transcriber(silence(0.1))
    speaker = listen.Speaker(client, Preferences(), tmp_path)
    speaker.mode = "server"
    with pytest.raises(VoiceError, match="not reachable"):
        await speaker.synthesize("Hello.")
