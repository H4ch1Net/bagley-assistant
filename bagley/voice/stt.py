"""Speech to text with whisper.cpp, entirely on this computer.

``transcribe(rt, audio, mime)`` takes what the browser's MediaRecorder sends (WebM or Ogg Opus,
MP4 from Safari) or a WAV, converts it to 16 kHz mono 16-bit with ffmpeg unless it already is,
runs whisper.cpp and returns the text without non-speech markers such as ``[BLANK_AUDIO]``.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from bagley.voice import Runner, VoiceError, VoiceUnavailable, find_program, last_line, run_program
from bagley.voice.audio import SPEECH_RATE, is_speech_wav, read_wav, sniff, wav_bytes

if TYPE_CHECKING:
    from bagley.runtime import Runtime

WHISPER_NAMES = ("whisper-cli", "whisper-cpp", "whisper", "main")
MAX_AUDIO_BYTES = 25_000_000
MAX_SECONDS = 300
FFMPEG_TIMEOUT = 30.0
WHISPER_TIMEOUT = 120.0
SYSTEM_SHARE = [Path("/usr/share")]
# Smaller English models first: fast enough for commands on a laptop CPU.
MODEL_ORDER = ("base.en", "small.en", "base", "small", "tiny.en", "tiny", "medium.en", "medium")

_TIMESTAMP = re.compile(r"^\s*\[\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}\]")
_MARKER = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫]+")
_LOG_LINE = re.compile(r"^(whisper_|main:|system_info|ggml_|output_txt|WARNING:)")


def whisper_binary() -> str | None:
    """whisper.cpp's command: ``whisper-cli`` today, ``main`` in old builds."""
    return find_program(*WHISPER_NAMES)


def model_dirs(data_dir: Path) -> list[Path]:
    share = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    dirs = [Path(data_dir) / "models", Path(data_dir) / "whisper"]
    dirs += [share / "whisper", share / "whisper.cpp"]
    for system in SYSTEM_SHARE:
        dirs += [system / "whisper.cpp", system / "whisper.cpp" / "models"]
        with contextlib.suppress(OSError):
            dirs += sorted(system.glob("whisper.cpp-model-*"))  # AUR model packages.
    return dirs


def _model_rank(path: Path) -> tuple[int, str]:
    size = path.name.removeprefix("ggml-").removesuffix(".bin")
    return (MODEL_ORDER.index(size) if size in MODEL_ORDER else len(MODEL_ORDER)), path.name


def find_models(data_dir: Path) -> list[Path]:
    """ggml models in ``<data dir>/models``, ``~/.local/share/whisper`` and system folders."""
    found: dict[str, Path] = {}
    for root in model_dirs(data_dir):
        try:
            for path in root.glob("ggml-*.bin"):
                if path.is_file() and "silero" not in path.name:
                    found.setdefault(path.name, path)
        except OSError:
            continue
    return sorted(found.values(), key=_model_rank)


def resolve_model(setting: str, data_dir: Path) -> Path | None:
    """The model for the ``whisper_model`` preference: a path, a name such as ``base.en`` or
    ``ggml-small.bin``, or empty for the best model found."""
    setting = setting.strip()
    if setting and ("/" in setting or "\\" in setting):
        path = Path(setting).expanduser()
        return path if path.is_file() else None
    models = find_models(data_dir)
    if not setting:
        return models[0] if models else None
    names = {setting, f"ggml-{setting}.bin", f"{setting}.bin"}
    return next((p for p in models if p.name in names), None)


def parse_output(raw: str) -> str:
    """whisper.cpp's text without timestamps, log lines or markers like ``[BLANK_AUDIO]``."""
    parts = []
    for line in raw.splitlines():
        if _LOG_LINE.match(line.strip()):
            continue
        line = _MARKER.sub(" ", _TIMESTAMP.sub("", line)).strip()
        if line:
            parts.append(line)
    return " ".join(" ".join(parts).split()).strip(" -")


async def to_speech_wav(audio: bytes, mime: str = "", *, runner: Runner | None = None) -> bytes:
    """``audio`` as a 16 kHz mono 16-bit WAV of at most ``MAX_SECONDS``. ffmpeg converts
    anything else; it only gets the demuxer the bytes announce, and only the pipe protocol,
    so a crafted playlist can't make it open files or URLs."""
    if is_speech_wav(audio):
        info = read_wav(audio)
        assert info is not None
        return wav_bytes(info[3][: MAX_SECONDS * SPEECH_RATE * 2], SPEECH_RATE)
    demuxer = sniff(audio)
    if demuxer is None:
        hint = f" ({mime})" if mime else ""
        raise VoiceError(f"Unsupported audio{hint}. Send WAV, WebM, Ogg, MP4, FLAC or MP3.")
    ffmpeg = find_program("ffmpeg")
    if not ffmpeg:
        raise VoiceUnavailable(f"ffmpeg is needed to convert {demuxer} audio for whisper.cpp.")
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "pipe",
        "-f",
        demuxer,
        "-i",
        "pipe:0",
        "-t",
        str(MAX_SECONDS),
        "-ar",
        str(SPEECH_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
    ]
    code, out, err = await (runner or run_program)(args, audio, FFMPEG_TIMEOUT)
    if code != 0:
        raise VoiceError(f"ffmpeg could not read the audio: {last_line(err)}")
    info = read_wav(out)
    if info is None or not info[3]:
        raise VoiceError("ffmpeg produced no audio.")
    return wav_bytes(info[3], info[0], info[1], info[2])  # Fixes the sizes of a piped header.


async def transcribe_audio(
    audio: bytes,
    mime: str = "",
    *,
    model: Path,
    binary: str | None = None,
    language: str = "auto",
    runner: Runner | None = None,
) -> str:
    """Transcribe ``audio`` with whisper.cpp and ``model``."""
    if not audio:
        raise VoiceError("No audio.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError(f"Audio up to {MAX_AUDIO_BYTES // 1_000_000} MB is accepted.")
    binary = binary or whisper_binary()
    if not binary:
        raise VoiceUnavailable("whisper.cpp is not installed (see docs/voice.md).")
    if not model.is_file():
        raise VoiceUnavailable(f"Whisper model not found: {model}")
    if not re.fullmatch(r"auto|[a-z]{2,3}", language):
        raise VoiceError("Language must be auto or a code such as en.")
    runner = runner or run_program
    wav = await to_speech_wav(audio, mime, runner=runner)
    with tempfile.TemporaryDirectory(prefix="bagley-stt-") as tmp:
        path = Path(tmp) / "speech.wav"
        path.write_bytes(wav)
        args = [binary, "-m", str(model), "-f", str(path), "-nt", "-np", "-l", language, "-otxt"]
        code, out, err = await runner(args, None, WHISPER_TIMEOUT)
        if code != 0:
            raise VoiceError(f"whisper.cpp failed: {last_line(err or out)}")
        written = Path(f"{path}.txt")  # -otxt writes next to the input.
        raw = (
            written.read_text(encoding="utf-8", errors="replace")
            if written.is_file()
            else out.decode("utf-8", "replace")
        )
    return parse_output(raw)


async def transcribe(
    rt: Runtime, audio: bytes, mime: str = "", *, runner: Runner | None = None
) -> str:
    """Transcribe ``audio`` with whisper.cpp and the model from the preferences.

    Raises ``VoiceUnavailable`` when whisper.cpp, its model or ffmpeg is missing, and
    ``VoiceError`` when the audio can't be read."""
    prefs, _ = rt.preferences()
    binary = whisper_binary()
    if not binary:
        raise VoiceUnavailable("whisper.cpp is not installed (see docs/voice.md).")
    model = resolve_model(prefs.whisper_model, rt.config.data_dir)
    if model is None:
        raise VoiceUnavailable(
            f"Whisper model not found: {prefs.whisper_model}"
            if prefs.whisper_model
            else "No whisper model found. Download ggml-base.en.bin (see docs/voice.md)."
        )
    return await transcribe_audio(audio, mime, model=model, binary=binary, runner=runner)
