"""Hearing the wake word: an energy voice activity detector and fuzzy wake word matching.

Both are pure Python on 16 kHz mono 16-bit PCM, so they run anywhere and test without a
microphone (``audioop`` is gone in Python 3.13; ``array`` does the arithmetic).

The VAD cuts the stream into 30 ms frames and compares each frame's loudness with a noise floor
that follows the room: it falls quickly when the room gets quieter and rises slowly when it gets
louder. Speech starts after ~200 ms above the floor and ends after ~700 ms below it; an
utterance never runs past 15 s, and a sound that loud for that long (a fan, music) becomes the
new floor.

Wake word matching looks only at the first words of an utterance, after fillers like "hey" or
"okay". The usual mis-hearings (bagly, badgley, baguely, bag lee) count as they are, and so does
anything at least 0.8 similar to the wake word (Bailey and Begley score 0.83, Bagley's 0.92).
"bagel" is not "bagley": it scores 0.73, like "badly" and "baggy"; "Bradley" scores 0.77.
"""

from __future__ import annotations

import math
import re
from collections import deque
from difflib import SequenceMatcher

from bagley.voice.audio import SPEECH_RATE, samples

FRAME_MS = 30
MISHEARINGS = ("bagley", "bagly", "badgley", "baguely", "bag lee", "hey bagley")
FILLERS = {"hey", "hi", "hello", "ok", "okay", "oi", "oy", "yo", "right", "so", "um", "uh", "er"}
SIMILARITY = 0.8
_WORD = re.compile(r"[\w'’]+")
_LEAD = " \t,.!?:;-—–…"


def rms(frame: bytes) -> float:
    """Loudness of 16-bit PCM as root mean square (0 to 32768)."""
    values = samples(frame)
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))


class VAD:
    """Finds utterances in a stream of PCM: ``feed`` returns the ones that just ended."""

    def __init__(
        self,
        *,
        rate: int = SPEECH_RATE,
        start_ms: int = 200,
        end_ms: int = 700,
        max_ms: int = 15_000,
        preroll_ms: int = 300,
        ratio: float = 3.0,
        min_level: float = 300.0,
    ) -> None:
        self.frame_bytes = rate * FRAME_MS // 1000 * 2
        self.start_frames = max(1, start_ms // FRAME_MS)
        self.end_frames = max(1, end_ms // FRAME_MS)
        self.max_frames = max(self.start_frames + 1, max_ms // FRAME_MS)
        self.ratio = ratio  # Speech is this many times louder than the noise floor...
        self.min_level = min_level  # ...and at least this loud (about -40 dBFS).
        self.floor: float | None = None
        self.level = 0.0  # The last frame's loudness, for meters.
        self.speaking = False
        self._buffer = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=max(1, preroll_ms // FRAME_MS))
        self._recent: deque[float] = deque(maxlen=max(1, 1000 // FRAME_MS))
        self._frames: list[bytes] = []
        self._voiced = 0
        self._silent = 0

    @property
    def threshold(self) -> float:
        return max((self.floor or 0.0) * self.ratio, self.min_level)

    def feed(self, pcm: bytes) -> list[bytes]:
        self._buffer += pcm
        done = []
        while len(self._buffer) >= self.frame_bytes:
            frame = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            utterance = self._frame(frame)
            if utterance:
                done.append(utterance)
        return done

    def reset(self) -> None:
        """Forget audio in flight (after Bagley spoke); the noise floor stays."""
        self._buffer.clear()
        self._preroll.clear()
        self._frames = []
        self._voiced = self._silent = 0
        self.speaking = False

    def _frame(self, frame: bytes) -> bytes | None:
        level = self.level = rms(frame)
        self._recent.append(level)
        if self.floor is None:
            self.floor = level
        if not self.speaking:
            self._preroll.append(frame)
            if level > self.threshold:
                self._voiced += 1
                if self._voiced >= self.start_frames:
                    self.speaking = True
                    self._frames = list(self._preroll)
                    self._silent = 0
            else:
                self._voiced = 0
                self._adapt(level)
            return None
        self._frames.append(frame)
        # Hysteresis: once someone is talking, softer syllables still count.
        self._silent = 0 if level > self.threshold * 0.6 else self._silent + 1
        if self._silent >= self.end_frames:
            keep = len(self._frames) - max(0, self._silent - 200 // FRAME_MS)
            return self._finish(keep)
        if len(self._frames) >= self.max_frames:
            self.floor = max(self.floor, sum(self._recent) / len(self._recent))
            return self._finish(len(self._frames))
        return None

    def _adapt(self, level: float) -> None:
        assert self.floor is not None
        rate = 0.2 if level < self.floor else 0.02
        self.floor += (level - self.floor) * rate

    def _finish(self, keep: int) -> bytes:
        utterance = b"".join(self._frames[:keep])
        self.reset()
        return utterance


# Wake word --------------------------------------------------------------------------------------


def _norm(word: str) -> str:
    return word.lower().replace("’", "").replace("'", "")


def compact(phrase: str) -> str:
    """A phrase as one lowercase word: "Bag Lee" -> "baglee"."""
    return "".join(_norm(w) for w in _WORD.findall(phrase))


def variants(wake_word: str) -> set[str]:
    """Spellings accepted as they are: the wake word and, for "bagley", the usual
    mis-hearings. Anything else must be similar to the wake word itself."""
    base = compact(wake_word) or "bagley"
    found = {base}
    if base == "bagley":
        found |= {compact(m) for m in MISHEARINGS}
    return found


def similar(heard: str, wake_word: str) -> bool:
    """Whether ``heard`` (compacted) is the wake word, a known mis-hearing or close to it."""
    if heard in variants(wake_word):
        return True
    return SequenceMatcher(None, heard, compact(wake_word) or "bagley").ratio() >= SIMILARITY


def detect(text: str, wake_word: str = "bagley") -> str | None:
    """The command after the wake word. None when the utterance doesn't start with the wake
    word (fillers such as "hey" or "okay" may come first), "" when it is only the wake word."""
    words = [(_norm(m.group(0)), m.end()) for m in _WORD.finditer(text)]
    base = compact(wake_word) or "bagley"
    size = max(1, len(_WORD.findall(wake_word)))
    for skip in range(3):
        if skip and (skip > len(words) or words[skip - 1][0] not in FILLERS):
            break
        for span in sorted({max(1, size - 1), size, size + 1}):
            if skip + span > len(words):
                continue
            first = words[skip][0]
            if span > size and not (len(first) >= 2 and base.startswith(first[:2])):
                continue  # Join "bag lee", never "the bagley".
            heard = "".join(w for w, _ in words[skip : skip + span])
            if similar(heard, wake_word):
                return text[words[skip + span - 1][1] :].lstrip(_LEAD).strip()
    return None
