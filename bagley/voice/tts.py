"""Text to speech: Piper (offline, on this computer) or ElevenLabs (online).

``synthesize(rt, text)`` speaks with the engine chosen in the preferences and returns
``(audio bytes, mime type)``: ``audio/wav`` from Piper, ``audio/mpeg`` from ElevenLabs. Text is
made speakable first and capped at ``MAX_CHARS``; the last ``CACHE_SIZE`` results stay in
memory, so replaying a reply costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from bagley.voice import Runner, VoiceError, VoiceUnavailable, find_program, last_line, run_program
from bagley.voice.audio import wav_bytes
from bagley.voice.text import in_character, limit_text, speakable

if TYPE_CHECKING:
    from bagley.runtime import Runtime

MAX_CHARS = 2500  # Per request.
CACHE_SIZE = 20
PIPER_TIMEOUT = 90.0
PIPER_ARGS = ["--length_scale", "0.95", "--sentence_silence", "0.25"]
DEFAULT_SAMPLE_RATE = 22_050
MAX_VOICES = 200
SYSTEM_VOICES = [Path("/usr/share/piper-voices")]  # AUR piper-voices-* packages.

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
ELEVENLABS_FORMAT = "mp3_44100_128"
ELEVENLABS_MODEL = "eleven_multilingual_v2"
ELEVENLABS_TIMEOUT = 60.0
DEFAULT_ELEVENLABS_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # "George": warm, refined, British.
VOICE_SETTINGS = {"stability": 0.45, "similarity_boost": 0.8, "style": 0.2}
VOICE_ID = re.compile(r"^[A-Za-z0-9]{8,40}$")

_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB"
RECOMMENDED_VOICES: list[dict[str, str]] = [
    {
        "name": "en_GB-alan-medium",
        "description": "Measured southern English male. The closest to Bagley.",
        "model_url": f"{_HF}/alan/medium/en_GB-alan-medium.onnx",
        "config_url": f"{_HF}/alan/medium/en_GB-alan-medium.onnx.json",
    },
    {
        "name": "en_GB-northern_english_male-medium",
        "description": "Northern English male, a little warmer.",
        "model_url": f"{_HF}/northern_english_male/medium/en_GB-northern_english_male-medium.onnx",
        "config_url": (
            f"{_HF}/northern_english_male/medium/en_GB-northern_english_male-medium.onnx.json"
        ),
    },
]


class SpeechCache:
    """The most recent results, keyed by a hash of engine, voice and text."""

    def __init__(self, size: int = CACHE_SIZE) -> None:
        self.size = size
        self.items: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    def get(self, key: str) -> tuple[bytes, str] | None:
        hit = self.items.get(key)
        if hit is not None:
            self.items.move_to_end(key)
        return hit

    def put(self, key: str, value: tuple[bytes, str]) -> None:
        self.items[key] = value
        self.items.move_to_end(key)
        while len(self.items) > self.size:
            self.items.popitem(last=False)


def cache_key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def prepare(text: str, *, character: bool = True, max_chars: int = MAX_CHARS) -> str:
    """What the voice actually reads: speakable, in character, at most ``max_chars``."""
    spoken = speakable(text)
    if character:
        spoken = in_character(spoken)
    return limit_text(spoken, max_chars)


# Piper ------------------------------------------------------------------------------------------


def piper_binary() -> str | None:
    """Piper's command. Kali's and Debian's ``piper`` package is a gaming mouse configurator
    with the same name, so ``piper-tts`` comes first and a GTK ``piper`` is skipped."""
    for name in ("piper-tts", "piper"):
        path = find_program(name)
        if path and not _is_mouse_app(path):
            return path
    return None


def _is_mouse_app(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return b"ratbag" in head or b"gi.require_version" in head


def voice_dirs(data_dir: Path) -> list[Path]:
    share = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return [share / "piper", share / "piper-voices", Path(data_dir) / "voices", *SYSTEM_VOICES]


def _voice_config(voice: Path) -> dict[str, Any]:
    for path in (Path(f"{voice}.json"), voice.with_suffix(".json")):
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def sample_rate(voice: Path) -> int:
    """The voice's output rate from its ``.onnx.json`` (Piper's raw output carries none)."""
    audio = _voice_config(voice).get("audio")
    rate = audio.get("sample_rate") if isinstance(audio, dict) else None
    return int(rate) if isinstance(rate, int) and 8000 <= rate <= 96_000 else DEFAULT_SAMPLE_RATE


def _voice_rank(path: Path) -> tuple[int, str]:
    names = [v["name"] for v in RECOMMENDED_VOICES]
    if path.stem in names:
        return names.index(path.stem), path.stem
    return (len(names) if path.stem.startswith("en_GB") else len(names) + 1), path.stem


def _voice_paths(data_dir: Path) -> list[Path]:
    found: dict[str, Path] = {}
    for root in voice_dirs(data_dir):
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.onnx"):
                if path.is_file():
                    found.setdefault(path.stem, path)
                if len(found) >= MAX_VOICES:
                    break
        except OSError:
            continue
    return sorted(found.values(), key=_voice_rank)


def find_voices(data_dir: Path) -> list[dict[str, Any]]:
    """Piper voices in ``~/.local/share/piper``, ``<data dir>/voices`` and the system folder,
    recommended British voices first."""
    voices = []
    for path in _voice_paths(data_dir):
        config = _voice_config(path)
        language = config.get("language")
        audio = config.get("audio")
        voices.append(
            {
                "name": path.stem,
                "path": str(path),
                "language": language.get("code", "") if isinstance(language, dict) else "",
                "quality": audio.get("quality", "") if isinstance(audio, dict) else "",
                "sample_rate": sample_rate(path),
                "config": bool(config),
            }
        )
    return voices


def resolve_voice(setting: str, data_dir: Path) -> Path | None:
    """The voice file for the ``piper_voice`` preference: a path, a voice name such as
    ``en_GB-alan-medium``, or empty for the best voice found."""
    setting = setting.strip()
    if setting and ("/" in setting or "\\" in setting or setting.endswith(".onnx")):
        path = Path(setting).expanduser()
        return path if path.is_file() and path.suffix == ".onnx" else None
    voices = _voice_paths(data_dir)
    if not setting:
        return voices[0] if voices else None
    return next(
        (p for p in voices if p.stem == setting or p.stem.split("-")[1:2] == [setting]), None
    )


async def piper(
    text: str, voice: Path, *, binary: str | None = None, runner: Runner | None = None
) -> bytes:
    """Speak ``text`` with Piper and return a WAV. Raw PCM output is wrapped with the voice's
    sample rate; a Piper that writes WAV anyway is passed through."""
    binary = binary or piper_binary()
    if not binary:
        raise VoiceUnavailable("Piper is not installed. Install piper-tts (see docs/voice.md).")
    if not voice.is_file():
        raise VoiceUnavailable(f"Piper voice not found: {voice}")
    line = " ".join(text.split())  # Piper reads one utterance per line.
    if not line:
        raise VoiceError("Nothing to say.")
    args = [binary, "--model", str(voice), "--output_raw", *PIPER_ARGS]
    code, out, err = await (runner or run_program)(args, (line + "\n").encode(), PIPER_TIMEOUT)
    if code != 0:
        raise VoiceError(f"Piper failed: {last_line(err)}")
    if out.startswith(b"RIFF"):
        return out
    if len(out) < 2:
        raise VoiceError("Piper produced no audio.")
    return wav_bytes(out, sample_rate(voice))


# ElevenLabs -------------------------------------------------------------------------------------


async def elevenlabs(
    http: httpx.AsyncClient,
    key: str,
    voice_id: str,
    text: str,
    *,
    model: str = ELEVENLABS_MODEL,
) -> bytes:
    """Speak ``text`` with ElevenLabs and return MP3. The text leaves this computer."""
    if not key:
        raise VoiceUnavailable("Add an ElevenLabs API key in Settings, Voice.")
    voice_id = voice_id.strip() or DEFAULT_ELEVENLABS_VOICE
    if not VOICE_ID.match(voice_id):
        raise VoiceUnavailable("An ElevenLabs voice id is letters and digits only.")
    try:
        resp = await http.post(
            ELEVENLABS_URL.format(voice=voice_id),
            params={"output_format": ELEVENLABS_FORMAT},
            headers={"xi-api-key": key, "Accept": "audio/mpeg"},
            json={"text": text, "model_id": model, "voice_settings": VOICE_SETTINGS},
            timeout=ELEVENLABS_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise VoiceError(f"ElevenLabs is not reachable ({type(exc).__name__}).") from exc
    if resp.status_code >= 400:
        raise VoiceError(_elevenlabs_error(resp, voice_id).replace(key, "***"))
    kind = resp.headers.get("content-type", "")
    if not resp.content or "json" in kind or kind.startswith("text/"):
        raise VoiceError("ElevenLabs returned no audio.")
    return resp.content


def _elevenlabs_error(resp: httpx.Response, voice_id: str) -> str:
    status, message = "", ""
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, dict):
        status = str(detail.get("status") or detail.get("code") or "")
        message = str(detail.get("message") or "")
    elif isinstance(detail, str):
        message = detail
    message = " ".join(message.split())[:200]
    code = resp.status_code
    if "quota" in status or "credit" in status or code == 402:
        return f"ElevenLabs quota used up: {message or 'no characters left this period.'}"
    if code == 401:
        return "ElevenLabs rejected the API key (401). Check it in Settings, Voice."
    if code == 429:
        return "ElevenLabs is rate limiting requests (429). Try again in a moment."
    if code == 404 or "voice_not_found" in status:
        return f"ElevenLabs has no voice {voice_id}."
    return f"ElevenLabs answered {code}: {message or resp.reason_phrase}"


# Engine choice ----------------------------------------------------------------------------------


async def synthesize(rt: Runtime, text: str, *, runner: Runner | None = None) -> tuple[bytes, str]:
    """Speak ``text`` (Markdown is fine) with the preferred engine: ``(audio, mime)``.

    Raises ``VoiceUnavailable`` when the engine is the browser or isn't set up, and
    ``VoiceError`` when synthesis fails."""
    prefs, _ = rt.preferences()
    engine = prefs.tts_engine
    if engine == "browser":
        raise VoiceUnavailable("Speech uses the browser's voice. Choose Piper or ElevenLabs.")
    spoken = prepare(text, character=prefs.persona == "bagley")
    if not spoken:
        raise VoiceError("Nothing to say.")
    cache: SpeechCache = rt.services.setdefault("voice.tts_cache", SpeechCache())
    if engine == "piper":
        voice = resolve_voice(prefs.piper_voice, rt.config.data_dir)
        if voice is None:
            raise VoiceUnavailable(
                f"Piper voice not found: {prefs.piper_voice}"
                if prefs.piper_voice
                else "No Piper voice found. Download en_GB-alan-medium (see docs/voice.md)."
            )
        key = cache_key("piper", str(voice), spoken)
        if hit := cache.get(key):
            return hit
        result = (await piper(spoken, voice, runner=runner), "audio/wav")
    else:
        voice_id = prefs.elevenlabs_voice.strip() or DEFAULT_ELEVENLABS_VOICE
        key = cache_key("elevenlabs", voice_id, ELEVENLABS_MODEL, spoken)
        if hit := cache.get(key):
            return hit
        audio = await elevenlabs(rt.http, prefs.elevenlabs_key, voice_id, spoken)
        result = (audio, "audio/mpeg")
    cache.put(key, result)
    return result
