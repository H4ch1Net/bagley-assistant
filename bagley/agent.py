"""The agent loop: stream a reply, run the tools it asks for, feed results back, repeat."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from bagley.llm import LLMError, Provider, ToolCall, ToolsUnsupportedError
from bagley.llm.textparse import StreamParser
from bagley.prompts import TITLE_PROMPT, clean_title, heuristic_title, system_prompt, to_prompt_mode
from bagley.runtime import Runtime
from bagley.tools import Tool, ToolError

log = logging.getLogger("bagley.agent")

Emit = Callable[[dict[str, Any]], Awaitable[None]]
Approve = Callable[[ToolCall, Tool], Awaitable[bool]]
ToolMode = Literal["native", "prompt", "off"]

RESULT_PREVIEW_CHARS = 4000


@dataclass
class RunRequest:
    text: str = ""
    conversation_id: str | None = None
    mode: Literal["send", "regenerate", "edit"] = "send"


@dataclass
class _Step:
    text: str = ""
    reasoning: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: Any = None
    first_token: float | None = None
    started: float = field(default_factory=time.monotonic)


def estimate_tokens(message: dict[str, Any]) -> int:
    size = len(message.get("content") or "")
    if message.get("tool_calls"):
        size += len(json.dumps(message["tool_calls"]))
    return size // 3 + 8


def _shrink(message: dict[str, Any], max_chars: int) -> dict[str, Any]:
    content = message.get("content") or ""
    if len(content) <= max_chars:
        return message
    cut = len(content) - max_chars
    return {
        **message,
        "content": f"{content[:max_chars]}\n…[{cut} characters trimmed to fit the context window]",
    }


def fit_history(history: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Choose the messages to send within ``budget`` tokens.

    The current turn (the last user message and everything after it) is always kept, with its
    user text and tool results trimmed if it is too big on its own. Older messages are added
    newest first while they fit, starting at a user message. Tool calls are only kept together
    with the results that directly follow them, which strict OpenAI-compatible servers require.
    """
    last_user = max((i for i, m in enumerate(history) if m["role"] == "user"), default=None)
    if last_user is None:
        return []
    older, current = history[:last_user], list(history[last_user:])
    if sum(map(estimate_tokens, current)) > budget:
        bulky = {i for i, m in enumerate(current) if m["role"] in ("user", "tool")}
        fixed = sum(estimate_tokens(m) for i, m in enumerate(current) if i not in bulky)
        share = max(400, (budget - fixed) * 3 // max(1, len(bulky)))
        current = [_shrink(m, share) if i in bulky else m for i, m in enumerate(current)]
    used = sum(map(estimate_tokens, current))
    kept: list[dict[str, Any]] = []
    for msg in reversed(older):
        used += estimate_tokens(msg)
        if used > budget:
            break
        kept.append(msg)
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return _pair_tool_calls(kept + current)


def _pair_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        content = m.get("content") or ""
        if m["role"] == "tool":  # A result without its call directly before it.
            i += 1
            continue
        if m["role"] == "assistant" and m.get("tool_calls"):
            j = i + 1
            results: dict[str, dict[str, Any]] = {}
            ids = {c["id"] for c in m["tool_calls"]}
            while j < len(messages) and messages[j]["role"] == "tool":
                r = messages[j]
                if r.get("tool_call_id") in ids and r["tool_call_id"] not in results:
                    results[r["tool_call_id"]] = r
                j += 1
            calls = [c for c in m["tool_calls"] if c["id"] in results]
            if calls:
                out.append({"role": "assistant", "content": content, "tool_calls": calls})
                out.extend(
                    {
                        "role": "tool",
                        "content": results[c["id"]].get("content") or "",
                        "tool_call_id": c["id"],
                        "name": results[c["id"]].get("name") or "",
                    }
                    for c in calls
                )
            elif content:
                out.append({"role": "assistant", "content": content})
            i = j
            continue
        out.append({"role": m["role"], "content": content})
        i += 1
    return out


class Agent:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self._background: set[asyncio.Task[None]] = set()

    async def run(self, req: RunRequest, emit: Emit, approve: Approve) -> None:
        """Answer one message. Only one run per conversation at a time, across all windows."""
        busy = self.rt.busy
        if req.conversation_id and req.conversation_id in busy:
            await emit(
                {"type": "error", "message": "This chat is already answering in another window."}
            )
            await emit(
                {
                    "type": "run.end",
                    "conversation_id": req.conversation_id,
                    "stopped": False,
                    "stats": {},
                }
            )
            return
        claimed: list[str] = []

        def claim(cid: str) -> None:
            busy.add(cid)
            claimed.append(cid)

        try:
            await self._run(req, emit, approve, claim)
        finally:
            for cid in claimed:
                busy.discard(cid)

    async def _run(
        self, req: RunRequest, emit: Emit, approve: Approve, claim: Callable[[str], None]
    ) -> None:
        rt = self.rt
        store = rt.store
        prefs, _ = rt.preferences()
        run_id = uuid.uuid4().hex[:8]
        started = time.monotonic()

        async def reject(message: str, cid: str | None = None) -> None:
            # Every run ends with run.end, so clients never stay stuck in "running".
            await emit({"type": "error", "message": message})
            await emit(
                {
                    "type": "run.end",
                    "run_id": run_id,
                    "conversation_id": cid,
                    "stopped": False,
                    "stats": {},
                }
            )

        if req.mode == "send" and not req.text.strip():
            await reject("Message is empty.")
            return
        conv = store.get_conversation(req.conversation_id) if req.conversation_id else None
        is_new = conv is None
        if conv is None:
            if req.mode != "send":
                await reject("That conversation no longer exists.")
                return
            conv = store.create_conversation(heuristic_title(req.text))
            await emit({"type": "conversation", "conversation": conv})
        cid = conv["id"]
        claim(cid)

        user_message = None
        history = store.list_messages(cid)
        last_user = next((m for m in reversed(history) if m["role"] == "user"), None)
        if req.mode == "send":
            user_message = store.add_message(cid, "user", req.text.strip())
        elif last_user is None:
            await reject("There is nothing to regenerate yet.", cid)
            return
        elif req.mode == "regenerate":
            store.delete_messages_from(cid, last_user["id"] + 1)
        else:  # edit
            store.delete_messages_from(cid, last_user["id"])
            user_message = store.add_message(cid, "user", req.text.strip() or last_user["content"])
        first_text = (user_message or last_user or {}).get("content", "")

        await emit(
            {
                "type": "run.start",
                "run_id": run_id,
                "conversation_id": cid,
                "mode": req.mode,
                "user_message": user_message,
            }
        )

        step = _Step()
        pending_calls: list[ToolCall] = []
        stats: dict[str, Any] = {"completion_tokens": 0}
        model = ""
        stopped = False
        try:
            provider = await rt.provider()
            model = await rt.resolve_model(provider, prefs)
            caps = await provider.capabilities(model)
            tools = rt.registry.enabled(prefs.disabled_tools) if prefs.tool_mode != "off" else []
            mode: ToolMode = "off"
            if tools:
                if prefs.tool_mode in ("native", "prompt"):
                    mode = prefs.tool_mode
                elif caps.tools is False or model in rt.prompt_mode_models:
                    mode = "prompt"
                else:
                    mode = "native"
            think = prefs.think if caps.thinking else None
            await emit({"type": "model", "model": model, "tool_mode": mode})

            for index in range(prefs.max_steps):
                messages = self._context(prefs, cid, tools, mode)
                await emit({"type": "status", "state": "thinking"})
                step = _Step()
                try:
                    await self._stream(
                        step, provider, model, messages, tools, mode, think, prefs, emit
                    )
                except ToolsUnsupportedError:
                    if mode != "native" or step.text or step.reasoning:
                        raise
                    mode = "prompt"
                    rt.prompt_mode_models.add(model)
                    await emit(
                        {
                            "type": "notice",
                            "message": f"{model} has no native tool support. Using text-based tool calls.",
                        }
                    )
                    messages = self._context(prefs, cid, tools, mode)
                    step = _Step()
                    await self._stream(
                        step, provider, model, messages, tools, mode, think, prefs, emit
                    )

                step_stats = self._step_stats(step)
                stats["completion_tokens"] += step_stats["completion_tokens"]
                stats["tokens_per_second"] = step_stats["tokens_per_second"]
                stats["prompt_tokens"] = step_stats["prompt_tokens"]
                assistant = store.add_message(
                    cid,
                    "assistant",
                    step.text.strip(),
                    reasoning=step.reasoning.strip(),
                    tool_calls=[c.to_message() for c in step.calls] or None,
                    meta={"model": model, **{k: v for k, v in step_stats.items() if v is not None}},
                )
                # Hand the step over before the next await, so a stop during the send below
                # neither saves the text twice nor leaves its tool calls without results.
                pending_calls = list(step.calls)
                step = _Step()
                await emit({"type": "message", "message": assistant})
                if not pending_calls:
                    break
                while pending_calls:
                    await self._run_tool(cid, pending_calls[0], emit, approve, prefs.disabled_tools)
                    pending_calls.pop(0)
                if index == prefs.max_steps - 1:
                    await emit(
                        {
                            "type": "notice",
                            "message": f"Stopped after {prefs.max_steps} steps. "
                            "Ask me to continue or raise the step limit in Settings.",
                        }
                    )
        except asyncio.CancelledError:
            stopped = True
            self._save_partial(cid, step, pending_calls, model)
        except LLMError as exc:
            self._save_partial(cid, step, pending_calls, model)
            await emit({"type": "error", "message": exc.message, "hint": exc.hint})
        except Exception as exc:
            log.exception("Agent run failed")
            self._save_partial(cid, step, pending_calls, model)
            await emit({"type": "error", "message": f"Something went wrong: {exc}"})

        stats["duration_ms"] = round((time.monotonic() - started) * 1000)
        stats["context_tokens"] = prefs.context_tokens
        await emit(
            {
                "type": "run.end",
                "run_id": run_id,
                "conversation_id": cid,
                "stopped": stopped,
                "stats": stats,
            }
        )
        if is_new and prefs.smart_titles and model and not stopped:
            task = asyncio.create_task(self._smart_title(cid, first_text, model, emit))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    # Context --------------------------------------------------------------------------------

    def _context(
        self, prefs: Any, cid: str, tools: list[Tool], mode: ToolMode
    ) -> list[dict[str, Any]]:
        rt = self.rt
        system = system_prompt(
            prefs,
            tools=tools if mode != "off" else [],
            memories=rt.store.list_memories(),
            workspace=str(rt.config.workspace),
            prompt_mode=mode == "prompt",
        )
        reserve = min(2048, prefs.context_tokens // 4)
        specs = len(json.dumps([t.spec() for t in tools])) // 3 if mode == "native" else 0
        budget = max(512, prefs.context_tokens - reserve - len(system) // 3 - specs)
        history = fit_history(rt.store.list_messages(cid), budget)
        if mode != "native":
            history = to_prompt_mode(history)
        return [{"role": "system", "content": system}, *history]

    # Streaming ------------------------------------------------------------------------------

    async def _stream(
        self,
        step: _Step,
        provider: Provider,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        mode: ToolMode,
        think: bool | None,
        prefs: Any,
        emit: Emit,
    ) -> None:
        parser = StreamParser(parse_tools=mode != "off", known_tools={t.name for t in tools})
        state = ""

        async def handle(
            text: str = "", reasoning: str = "", calls: list[ToolCall] | None = None
        ) -> None:
            nonlocal state
            if reasoning:
                step.reasoning += reasoning
                if state != "reasoning":
                    state = "reasoning"
                    await emit({"type": "status", "state": "reasoning"})
                await emit({"type": "reasoning.delta", "text": reasoning})
            if text:
                if not step.text:
                    text = text.lstrip()
                if text:
                    step.text += text
                    if state != "writing":
                        state = "writing"
                        await emit({"type": "status", "state": "writing"})
                    await emit({"type": "text.delta", "text": text})
            if calls:
                step.calls.extend(calls)

        async for chunk in provider.chat(
            messages,
            model=model,
            tools=[t.spec() for t in tools] if mode == "native" else None,
            temperature=prefs.temperature,
            context_tokens=prefs.context_tokens,
            think=think,
        ):
            if step.first_token is None and (chunk.text or chunk.reasoning or chunk.tool_calls):
                step.first_token = time.monotonic()
            if chunk.reasoning:
                await handle(reasoning=chunk.reasoning)
            if chunk.text:
                parsed = parser.feed(chunk.text)
                await handle(parsed.text, parsed.reasoning, parsed.tool_calls)
            if chunk.tool_calls:
                await handle(calls=chunk.tool_calls)
            if chunk.usage:
                step.usage = chunk.usage
        tail = parser.finish()
        await handle(tail.text, tail.reasoning, tail.tool_calls)

    @staticmethod
    def _step_stats(step: _Step) -> dict[str, Any]:
        usage = step.usage
        tokens = usage.completion_tokens if usage and usage.completion_tokens else None
        if tokens is None:
            tokens = max(1, (len(step.text) + len(step.reasoning)) // 4)
        tps = usage.tokens_per_second if usage else None
        if tps is None and step.first_token:
            elapsed = time.monotonic() - step.first_token
            tps = tokens / elapsed if elapsed > 0.05 else None
        return {
            "completion_tokens": tokens,
            "tokens_per_second": round(tps, 1) if tps else None,
            "prompt_tokens": usage.prompt_tokens if usage else None,
        }

    # Tools ----------------------------------------------------------------------------------

    async def _run_tool(
        self, cid: str, call: ToolCall, emit: Emit, approve: Approve, disabled: list[str]
    ) -> None:
        tool = self.rt.registry.get(call.name)
        if tool and tool.name in disabled:
            tool = None
        await emit(
            {
                "type": "tool.start",
                "call": {"id": call.id, "name": call.name, "arguments": call.arguments},
                "summary": tool.summary if tool else "",
                "risk": tool.risk if tool else "safe",
                "category": tool.category if tool else "general",
            }
        )
        started = time.monotonic()
        ok = False
        if tool is None:
            names = ", ".join(t.name for t in self.rt.registry.enabled(disabled))
            result = f"Error: there is no tool named '{call.name}'. Available tools: {names}"
        else:
            allowed = True
            if tool.risk == "confirm":
                await emit({"type": "status", "state": "approval"})
                await emit(
                    {
                        "type": "approval.request",
                        "call": {"id": call.id, "name": call.name, "arguments": call.arguments},
                        "description": tool.description,
                    }
                )
                allowed = await approve(call, tool)
                await emit({"type": "approval.result", "id": call.id, "allowed": allowed})
            if not allowed:
                result = "The user declined this action. Don't retry it unless they ask."
            else:
                await emit({"type": "status", "state": "tool", "tool": call.name})
                started = time.monotonic()
                try:
                    result = await tool.invoke(call.arguments, self.rt.tool_context(cid))
                    ok = True
                except ToolError as exc:
                    result = f"Error: {exc}"
                except Exception as exc:
                    log.exception("Tool %s crashed", call.name)
                    result = f"Error: {exc.__class__.__name__}: {exc}"
        duration = round((time.monotonic() - started) * 1000)
        self.rt.store.add_message(
            cid,
            "tool",
            result,
            tool_call_id=call.id,
            name=call.name,
            meta={"ok": ok, "duration_ms": duration},
        )
        await emit(
            {
                "type": "tool.end",
                "id": call.id,
                "ok": ok,
                "result": result[:RESULT_PREVIEW_CHARS],
                "duration_ms": duration,
            }
        )

    def _save_partial(self, cid: str, step: _Step, pending: list[ToolCall], model: str) -> None:
        """Persist what was generated before a stop or error so the history stays consistent."""
        store = self.rt.store
        if step.text.strip() or step.reasoning.strip():
            store.add_message(
                cid,
                "assistant",
                step.text.strip(),
                reasoning=step.reasoning.strip(),
                meta={"model": model, "interrupted": True},
            )
        answered = {m["tool_call_id"] for m in store.list_messages(cid) if m["role"] == "tool"}
        for call in pending:
            if call.id in answered:
                continue
            store.add_message(
                cid,
                "tool",
                "Cancelled by the user.",
                tool_call_id=call.id,
                name=call.name,
                meta={"ok": False, "cancelled": True},
            )

    async def _smart_title(self, cid: str, text: str, model: str, emit: Emit) -> None:
        try:
            provider = await self.rt.provider()
            caps = await provider.capabilities(model)
            raw = await provider.complete(
                [{"role": "user", "content": TITLE_PROMPT.format(message=text[:600])}],
                model=model,
                temperature=0.3,
                max_tokens=40,
                think=False if caps.thinking else None,
            )
            parsed = StreamParser(parse_tools=False)
            body = parsed.feed(raw).text + parsed.finish().text
            title = clean_title(body)
            if title and self.rt.store.rename_conversation(cid, title):
                await emit({"type": "title", "conversation_id": cid, "title": title})
        except Exception as exc:  # Titles are cosmetic; never surface failures.
            log.debug("Title generation failed: %s", exc)
