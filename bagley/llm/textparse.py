"""Streaming parser for tags that some models emit inside plain text.

* ``<think>…</think>`` is routed to reasoning instead of the visible reply.
* ``<tool_call>{json}</tool_call>`` (Hermes/Qwen style) becomes a structured tool call. This is
  how "prompt mode" tool use works for models without native function calling, and it also
  rescues native-mode models that print tool calls as text.

Tags can arrive split across chunks, so any trailing text that could be the start of a tag is
held back until the next chunk decides it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bagley.llm.base import ToolCall, new_call_id

THINK_OPEN, THINK_CLOSE = "<think>", "</think>"
TOOL_OPEN, TOOL_CLOSE = "<tool_call>", "</tool_call>"

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_tool_call(raw: str, known: set[str] | None = None) -> ToolCall | None:
    """Parse the JSON body of a ``<tool_call>`` block. Returns ``None`` if it isn't one."""
    text = _FENCE.sub("", raw.strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("function"), dict):
        data = data["function"]
    name = data.get("name") or data.get("tool")
    args = data.get("arguments", data.get("parameters", data.get("args", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return None
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    if known is not None and name not in known:
        return None
    return ToolCall(id=new_call_id(), name=name, arguments=args)


def _partial_suffix(buf: str, tags: tuple[str, ...]) -> int:
    """Length of the longest suffix of ``buf`` that is a proper prefix of one of ``tags``."""
    best = 0
    for tag in tags:
        for n in range(min(len(tag) - 1, len(buf)), 0, -1):
            if buf.endswith(tag[:n]):
                best = max(best, n)
                break
    return best


@dataclass
class Parsed:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text or self.reasoning or self.tool_calls)


class StreamParser:
    def __init__(self, *, parse_tools: bool = True, known_tools: set[str] | None = None) -> None:
        self.parse_tools = parse_tools
        self.known_tools = known_tools
        self._state = "text"
        self._buf = ""

    @property
    def _open_tags(self) -> tuple[str, ...]:
        return (THINK_OPEN, TOOL_OPEN) if self.parse_tools else (THINK_OPEN,)

    def feed(self, chunk: str) -> Parsed:
        self._buf += chunk
        out = Parsed()
        while self._buf:
            if self._state == "text":
                hits = [(self._buf.find(t), t) for t in self._open_tags]
                hits = [(i, t) for i, t in hits if i >= 0]
                if hits:
                    i, tag = min(hits)
                    out.text += self._buf[:i]
                    self._buf = self._buf[i + len(tag) :]
                    self._state = "think" if tag == THINK_OPEN else "tool"
                    continue
                keep = _partial_suffix(self._buf, self._open_tags)
                out.text += self._buf[: len(self._buf) - keep]
                self._buf = self._buf[len(self._buf) - keep :]
                break
            if self._state == "think":
                i = self._buf.find(THINK_CLOSE)
                if i >= 0:
                    out.reasoning += self._buf[:i]
                    self._buf = self._buf[i + len(THINK_CLOSE) :].lstrip("\n")
                    self._state = "text"
                    continue
                keep = _partial_suffix(self._buf, (THINK_CLOSE,))
                out.reasoning += self._buf[: len(self._buf) - keep]
                self._buf = self._buf[len(self._buf) - keep :]
                break
            # Inside a tool call: wait for the closing tag.
            i = self._buf.find(TOOL_CLOSE)
            if i < 0:
                break
            self._emit_tool(self._buf[:i], out)
            self._buf = self._buf[i + len(TOOL_CLOSE) :]
            self._state = "text"
        return out

    def finish(self) -> Parsed:
        out = Parsed()
        if self._state == "text":
            out.text = self._buf
        elif self._state == "think":
            out.reasoning = self._buf
        else:  # Unterminated tool call: models often drop the closing tag.
            self._emit_tool(self._buf, out)
        self._buf, self._state = "", "text"
        return out

    def _emit_tool(self, body: str, out: Parsed) -> None:
        call = parse_tool_call(body, self.known_tools)
        if call:
            out.tool_calls.append(call)
        else:
            out.text += TOOL_OPEN + body + TOOL_CLOSE
