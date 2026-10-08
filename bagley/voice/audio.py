"""Audio plumbing without dependencies: WAV in and out, the ready tone, format sniffing and
playback through whichever player the desktop has (PipeWire, PulseAudio, ALSA or ffplay)."""

from __future__ import annotations

import io
import math
import struct
import sys
import tempfile
import wave
from array import array
from pathlib import Path

from bagley.voice import Runner, VoiceError, VoiceUnavailable, find_program, last_line, run_program

SPEECH_RATE = 16_000  # What whisper.cpp wants: 16 kHz, mono, 16-bit.
PLAY_TIMEOUT = 600.0

# Players in order of preference, by format. The file path is appended.
WAV_PLAYERS = [
    ["pw-play"],
    ["paplay"],
    ["aplay", "-q"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error"],
]
MP3_PLAYERS = [
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error"],
    ["mpv", "--no-video", "--really-quiet"],
    ["pw-play"],  # libsndfile 1.1+ reads MP3.
    ["paplay"],
]

# Container magic bytes and the ffmpeg demuxer that reads them.
FORMATS = [
    (b"RIFF", "wav"),
    (b"OggS", "ogg"),
    (b"\x1a\x45\xdf\xa3", "matroska"),  # WebM from MediaRecorder in Chrome and Firefox.
    (b"fLaC", "flac"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
]


def sniff(data: bytes) -> str | None:
    """The ffmpeg demuxer for ``data`` from its first bytes, or None if unknown."""
    if data[4:8] == b"ftyp":
        return "mov"  # MP4/M4A, e.g. Safari's MediaRecorder.
    for magic, name in FORMATS:
        if data.startswith(magic):
            if name == "wav" and data[8:12] != b"WAVE":
                return None
            return name
    return None


def wav_bytes(pcm: bytes, rate: int, channels: int = 1, width: int = 2) -> bytes:
    """Wrap raw little-endian PCM in a WAV header."""
    pcm = pcm[: len(pcm) - len(pcm) % (channels * width)]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[int, int, int, bytes] | None:
    """(sample rate, channels, sample width, PCM) of a PCM WAV, or None. Tolerates the unknown
    sizes ffmpeg and recorders write when they stream to a pipe."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos = 12
    fmt: tuple[int, int, int, int] | None = None
    while pos + 8 <= len(data):
        cid, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = pos + 8
        if cid == b"fmt " and size >= 16 and body + 16 <= len(data):
            tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", data[body : body + 16])
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            if fmt is None or fmt[0] not in (1, 0xFFFE) or not fmt[1] or not fmt[2]:
                return None
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            width = max(1, fmt[3] // 8)
            pcm = data[body:end]
            return fmt[2], fmt[1], width, pcm[: len(pcm) - len(pcm) % (width * fmt[1])]
        pos = body + size + (size & 1)
    return None


def is_speech_wav(data: bytes) -> bool:
    """Already 16 kHz mono 16-bit PCM, so whisper.cpp can read it without ffmpeg."""
    info = read_wav(data)
    return info is not None and info[:3] == (SPEECH_RATE, 1, 2)


def duration(data: bytes) -> float:
    """Seconds of audio in a WAV (0 for other formats)."""
    info = read_wav(data)
    if not info:
        return 0.0
    rate, channels, width, pcm = info
    return len(pcm) / (rate * channels * width)


def samples(pcm: bytes) -> array[int]:
    """16-bit little-endian PCM as integers."""
    values = array("h")
    values.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        values.byteswap()
    return values


def pcm_bytes(values: array[int]) -> bytes:
    if sys.byteorder == "big":
        values = array("h", values)
        values.byteswap()
    return values.tobytes()


def tone(freq: float = 880.0, ms: int = 120, rate: int = 22_050, volume: float = 0.3) -> bytes:
    """A short sine beep as a WAV, with 10 ms fades so it doesn't click."""
    count = rate * ms // 1000
    fade = max(1, rate // 100)
    amp = 32767 * max(0.0, min(volume, 1.0))
    values = array("h")
    for i in range(count):
        envelope = min(1.0, i / fade, (count - 1 - i) / fade)
        values.append(int(amp * envelope * math.sin(2 * math.pi * freq * i / rate)))
    return wav_bytes(pcm_bytes(values), rate)


def players(mime: str = "audio/wav") -> list[str]:
    """Players found on this computer for ``mime``, best first."""
    table = MP3_PLAYERS if "mpeg" in mime or "mp3" in mime else WAV_PLAYERS
    return [cmd[0] for cmd in table if find_program(cmd[0])]


def player_command(mime: str) -> list[str] | None:
    table = MP3_PLAYERS if "mpeg" in mime or "mp3" in mime else WAV_PLAYERS
    for cmd in table:
        path = find_program(cmd[0])
        if path:
            return [path, *cmd[1:]]
    return None


async def play(data: bytes, mime: str, *, runner: Runner | None = None) -> None:
    """Play audio and return when it has finished."""
    command = player_command(mime)
    if command is None:
        raise VoiceUnavailable("No audio player found (pw-play, paplay, aplay or ffplay).")
    suffix = ".mp3" if "mpeg" in mime or "mp3" in mime else ".wav"
    with tempfile.TemporaryDirectory(prefix="bagley-voice-") as tmp:
        path = Path(tmp) / f"speech{suffix}"
        path.write_bytes(data)
        code, _, err = await (runner or run_program)([*command, str(path)], None, PLAY_TIMEOUT)
    if code != 0:
        raise VoiceError(f"{Path(command[0]).name} failed: {last_line(err)}")
