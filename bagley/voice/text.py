"""Speakable text: Markdown replies turned into sentences a voice can read.

``speakable`` drops what can't be heard (code, tables, URLs, symbols), ``chunk`` cuts the result
into sentence groups for streaming synthesis, and ``in_character`` adds Bagley's dry pauses
without changing a single word.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

CHUNK_CHARS = 400
CODE_OMITTED = "(code omitted)"
TABLE_OMITTED = "(table omitted)"
INLINE_CODE_CHARS = 40  # Longer inline code is omitted too.

_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)", re.S | re.M)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_RULE = re.compile(r"^\s*([-*_=])(\s*\1){2,}\s*$")
_REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s*\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?")
_QUOTE = re.compile(r"^\s*(?:>\s?)+")

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_FOOTNOTE = re.compile(r"\[\^[^\]]+\]")
_AUTOLINK = re.compile(r"<((?:https?|ftp)://[^>\s]+)>")
_URL = re.compile(r"\b(?:https?|ftp)://[^\s<>()\[\]]+|\bwww\.[^\s<>()\[\]]+")
_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_INLINE_CODE = re.compile(r"`+([^`]+)`+")
_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_EM = re.compile(r"(?<![\w*])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*])")
_STRIKE = re.compile(r"~~(.+?)~~")
_PICTOGRAPHS = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U00002b00-\U00002bff"
    "\U00002190-\U000021ff\U00002500-\U0000259f️‍⃣]"
)
_SYMBOLS = re.compile(r"[#*_`~|<>{}\[\]\\^]")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?%)])")

ABBREVIATIONS = {
    "mr.",
    "mrs.",
    "ms.",
    "dr.",
    "prof.",
    "st.",
    "sr.",
    "jr.",
    "vs.",
    "e.g.",
    "i.e.",
    "approx.",
    "fig.",
}
_SENTENCE_GAP = re.compile(r"[.!?…]+[\"'”’)\]]*\s+")


def speakable(markdown: str) -> str:
    """Markdown as plain sentences for a voice: code blocks become "(code omitted)", links
    become their text, tables become their headers, symbols go, whitespace collapses."""
    text = _COMMENT.sub("", markdown.replace("\r\n", "\n").replace("\r", "\n"))
    text = _FENCE.sub(f"\n\n{CODE_OMITTED}\n\n", text)
    blocks: list[str] = []
    paragraph: list[str] = []
    quoting = False  # The open paragraph is a block quote.

    def flush() -> None:
        if paragraph:
            blocks.append(" ".join(paragraph))
            paragraph.clear()

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
            flush()
            headers = [_inline(c) for c in line.strip().strip("|").split("|")]
            headers = [h for h in headers if h][:6]
            blocks.append(f"Table: {', '.join(headers)}" if headers else TABLE_OMITTED)
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                i += 1
            continue
        i += 1
        if not line.strip() or _RULE.match(line) or _REF_DEF.match(line):
            flush()
            continue
        if heading := _HEADING.match(line):
            flush()
            blocks.append(heading.group(1))
            continue
        quote = bool(_QUOTE.match(line))
        if quote != quoting:
            flush()
            quoting = quote
        line = _QUOTE.sub("", line)
        if _ITEM.match(line):
            flush()
            paragraph.append(_ITEM.sub("", line, count=1))
            continue
        paragraph.append(line.strip())
    flush()

    sentences = []
    for block in blocks:
        spoken = _inline(block)
        if not spoken:
            continue
        if spoken[-1] not in ".!?…:;,":
            spoken += "."
        if sentences and spoken == sentences[-1] == f"{CODE_OMITTED}.":
            continue
        sentences.append(spoken)
    return " ".join(sentences)


def _host(match: re.Match[str]) -> str:
    url = match.group(1) if match.re is _AUTOLINK else match.group(0)
    if url.startswith("www."):
        url = "http://" + url
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return host or "a link"


def _inline(text: str) -> str:
    text = _IMAGE.sub(lambda m: m.group(1), text)
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _REF_LINK.sub(lambda m: m.group(1), text)
    text = _FOOTNOTE.sub("", text)
    text = _AUTOLINK.sub(_host, text)
    text = _URL.sub(_host, text)
    text = _HTML.sub(" ", text)
    text = _INLINE_CODE.sub(
        lambda m: m.group(1) if len(m.group(1)) <= INLINE_CODE_CHARS else CODE_OMITTED, text
    )
    for _ in range(2):  # Nested emphasis, e.g. ***both***.
        text = _STRONG.sub(lambda m: m.group(2), text)
        text = _EM.sub(lambda m: m.group(2), text)
    text = _STRIKE.sub(lambda m: m.group(1), text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s*(?:->|=>|→|⟶)\s*", " to ", text)
    text = re.sub(r"\s+&\s+", " and ", text)
    text = re.sub(r"\s*[·•]\s*", ", ", text)
    text = re.sub(r"(?<!\d)/|/(?!\d)", " ", text)  # Paths and "and/or", not "1/2".
    text = _PICTOGRAPHS.sub("", text)
    text = _SYMBOLS.sub(" ", text)
    text = " ".join(text.split())
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    return text.strip(" ,;")


def sentences(text: str) -> list[str]:
    """Split text into sentences, keeping abbreviations like "e.g." and "Dr." intact."""
    found: list[str] = []
    start = 0
    for match in _SENTENCE_GAP.finditer(text):
        following = text[match.end() : match.end() + 1]
        if not (following.isupper() or following.isdigit() or following in "\"'“‘(["):
            continue
        word = text[start : match.start() + 1].rsplit(None, 1)[-1].lower()
        if word in ABBREVIATIONS:
            continue
        found.append(text[start : match.end()].strip())
        start = match.end()
    tail = text[start:].strip()
    if tail:
        found.append(tail)
    return found


def _split_long(sentence: str, limit: int) -> list[str]:
    """Cut one over-long sentence at commas and semicolons, then between words."""
    if len(sentence) <= limit:
        return [sentence]
    parts: list[str] = []
    for clause in re.split(r"(?<=[,;:])\s+", sentence):
        words = clause.split() if len(clause) > limit else [clause]
        for word in words:
            while len(word) > limit:  # One absurd token.
                parts.append(word[:limit])
                word = word[limit:]
            if parts and len(parts[-1]) + 1 + len(word) <= limit:
                parts[-1] += " " + word
            else:
                parts.append(word)
    return parts


def chunk(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Group sentences into pieces of at most ``limit`` characters, in order."""
    pieces: list[str] = []
    current = ""
    for sentence in sentences(" ".join(text.split())):
        for part in _split_long(sentence, limit):
            if current and len(current) + 1 + len(part) > limit:
                pieces.append(current)
                current = part
            else:
                current = f"{current} {part}" if current else part
    if current:
        pieces.append(current)
    return pieces


def limit_text(text: str, max_chars: int) -> str:
    """At most ``max_chars``, cut at the end of a sentence where possible."""
    if len(text) <= max_chars:
        return text
    kept = ""
    for piece in chunk(text, min(CHUNK_CHARS, max_chars)):
        if len(kept) + 1 + len(piece) > max_chars:
            break
        kept = f"{kept} {piece}" if kept else piece
    return kept


# Sentence adverbs Bagley lets hang for a beat ("Naturally, I've already done it.").
_ASIDES = (
    "naturally",
    "obviously",
    "of course",
    "honestly",
    "frankly",
    "apparently",
    "unfortunately",
    "fortunately",
    "admittedly",
    "anyway",
    "incidentally",
    "regrettably",
    "evidently",
    "alas",
    "sadly",
    "mind you",
    "rest assured",
)
_ASIDE = re.compile(
    r"(?:(?<=^)|(?<=[.!?]\s))("
    + "|".join(re.escape(a) for a in _ASIDES)
    + r")(?=\s+(?:i|i'm|i've|you|you're|we|they|he|she|it|it's|that|this|there|the|your|my|a|an)\b)",
    re.I,
)
_PAREN = re.compile(r"(?<=\w)\s+\(([^()]{1,80})\)")
_DASH = re.compile(r"\s*(?:—|–|\s-\s)\s*")


def in_character(text: str) -> str:
    """Bagley's delivery: dry, unhurried, never exclaiming. Only punctuation changes; every
    word stays exactly as written."""
    text = text.replace("…", "...")
    text = re.sub(r"\?!+|!+\?", "?", text)
    text = re.sub(r"!+", ".", text)
    text = _ASIDE.sub(r"\1,", text)
    text = _PAREN.sub(r", \1,", text)
    text = _DASH.sub(", ", text)
    text = re.sub(r",\s*([.,;:?])", r"\1", text)
    return " ".join(text.split())
