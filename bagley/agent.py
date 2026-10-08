"""The agent loop: stream a reply, run the tools it asks for, feed results back, repeat."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from bagley.llm import LLMError, Provider, ToolCall, ToolsUnsupportedError, UnreachableError
from bagley.llm.textparse import StreamParser
from bagley.modes import get_mode
from bagley.permissions import denied, permission_for
from bagley.policy import UnattendedPolicy
from bagley.prompts import TITLE_PROMPT, clean_title, heuristic_title, system_prompt, to_prompt_mode
from bagley.routing import Route
from bagley.runtime import Runtime
from bagley.toolroute import select_tools
from bagley.tools import Tool, ToolError

log = logging.getLogger("bagley.agent")

Emit = Callable[[dict[str, Any]], Awaitable[None]]
Approve = Callable[[ToolCall, Tool], Awaitable[bool]]
ToolMode = Literal["native", "prompt", "off"]

RESULT_PREVIEW_CHARS = 4000
IMAGE_TOKENS = 800  # Rough context cost of one image.
CAPTURE_NAME = re.compile(r"^[\w.-]{1,80}\.(png|jpe?g|webp|gif)$")


@dataclass
class RunRequest:
    text: str = ""
    conversation_id: str | None = None
    mode: Literal["send", "regenerate", "edit"] = "send"
    tools: bool = True
    policy: UnattendedPolicy | None = None  # Set for runs nobody is watching.
    source: str = "web"  # web, cli, overlay, shell, voice, phone, automation, routine
    images: list[str] = field(default_factory=list)  # Capture file names for the vision model.
    conversation_mode: str | None = None  # Mode of a conversation this run creates.
    user_meta: dict[str, Any] = field(default_factory=dict)  # Stored with the user message.


@dataclass
class _RunInfo:
    cid: str
    source: str
    prefs: Any
    policy: UnattendedPolicy | None = None


@dataclass
class _Step:
    text: str = ""
    reasoning: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: Any = None
    first_token: float | None = None
    started: float = field(default_factory=time.monotonic)
    native: dict[str, Any] | None = None


def public_message(message: dict[str, Any]) -> dict[str, Any]:
    """A stored message without the provider's own copy of the reply (opaque thinking blocks)."""
    if "native" not in (message.get("meta") or {}):
        return message
    meta = {k: v for k, v in message["meta"].items() if k != "native"}
    return {**message, "meta": meta}


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
                reply = {"role": "assistant", "content": content, "tool_calls": calls}
                if m.get("native") and len(calls) == len(m["tool_calls"]):
                    reply["native"] = m["native"]
                out.append(reply)
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
        if m.get("native"):
            out[-1]["native"] = m["native"]
        i += 1
    return out


class Agent:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self._background: set[asyncio.Task[None]] = set()
        self._offered: dict[str, Tool] = {}  # Tools offered this step, incl. load_tools.

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

        run_id = uuid.uuid4().hex[:8]
        activity = self.rt.activity
        outcome = {"failed": False, "stopped": False}

        async def tracked(event: dict[str, Any]) -> None:
            kind = event["type"]
            if kind == "status":
                await activity.update(run_id, state=event["state"], tool=event.get("tool", ""))
            elif kind == "run.start":
                await activity.update(
                    run_id, conversation_id=event["conversation_id"], source=req.source
                )
            elif kind == "model":
                await activity.update(
                    run_id, machine=event.get("machine", ""), model=event.get("model", "")
                )
            elif kind == "error":
                outcome["failed"] = True
            elif kind == "run.end":
                outcome["stopped"] = bool(event.get("stopped"))
            await emit(event)

        try:
            await self._run(req, tracked, approve, claim, run_id)
        finally:
            for cid in claimed:
                busy.discard(cid)
            await activity.end(run_id, failed=outcome["failed"], stopped=outcome["stopped"])

    async def _run(
        self,
        req: RunRequest,
        emit: Emit,
        approve: Approve,
        claim: Callable[[str], None],
        run_id: str,
    ) -> None:
        rt = self.rt
        store = rt.store
        prefs, _ = rt.preferences()
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
            conv = store.create_conversation(
                heuristic_title(req.text),
                mode=get_mode(req.conversation_mode or prefs.default_mode).id,
            )
            await emit({"type": "conversation", "conversation": conv})
        cid = conv["id"]
        claim(cid)

        user_message = None
        history = store.list_messages(cid)
        last_user = next((m for m in reversed(history) if m["role"] == "user"), None)
        user_meta = dict(req.user_meta)
        images = [name for name in req.images if CAPTURE_NAME.match(name)]
        if images:
            user_meta["images"] = images
        if req.source != "web":
            user_meta.setdefault("source", req.source)
        if req.mode == "send":
            user_message = store.add_message(cid, "user", req.text.strip(), meta=user_meta or None)
        elif last_user is None:
            await reject("There is nothing to regenerate yet.", cid)
            return
        elif req.mode == "regenerate":
            store.delete_messages_from(cid, last_user["id"] + 1)
        else:  # edit
            store.delete_messages_from(cid, last_user["id"])
            user_message = store.add_message(
                cid,
                "user",
                req.text.strip() or last_user["content"],
                meta=user_meta or last_user.get("meta") or None,
            )
        first_text = (user_message or last_user or {}).get("content", "")
        turn_images = ((user_message or last_user or {}).get("meta") or {}).get("images") or []
        conv_mode = get_mode((store.get_conversation(cid) or {}).get("mode"))
        info = _RunInfo(cid=cid, source=req.source, prefs=prefs, policy=req.policy)

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
        route: Route | None = None
        stopped = False
        try:
            purpose = "vision" if turn_images else "chat"
            route = await rt.router.choose(prefs, purpose)
            tried: set[str] = {route.machine.id}
            tools = (
                rt.registry.enabled(denied(prefs)) if prefs.tool_mode != "off" and req.tools else []
            )
            mode, think = await self._setup(route, prefs, tools, emit)
            provider, model = route.provider, route.model

            for index in range(prefs.max_steps):
                offered = tools
                if prefs.tool_routing and tools:
                    offered = select_tools(tools, store.list_messages(cid), conv_mode.groups)
                    self._offered = {t.name: t for t in offered}
                messages = self._context(prefs, cid, offered, mode, conv_mode.id, provider)
                await emit({"type": "status", "state": "thinking"})
                while True:
                    step = _Step()
                    try:
                        await self._stream(
                            step, provider, model, messages, offered, mode, think, prefs, emit
                        )
                        break
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
                        messages = self._context(prefs, cid, offered, mode, conv_mode.id, provider)
                    except UnreachableError:
                        # Nothing streamed yet: let the next machine take over.
                        if step.text or step.reasoning or step.calls:
                            raise
                        next_route = await self._failover(route, prefs, purpose, tried)
                        if next_route is None:
                            raise
                        await emit(
                            {
                                "type": "notice",
                                "message": f"{route.machine.name} is not answering. "
                                f"Switching to {next_route.machine.name}.",
                            }
                        )
                        route = next_route
                        tried.add(route.machine.id)
                        mode, think = await self._setup(route, prefs, tools, emit)
                        provider, model = route.provider, route.model
                        messages = self._context(prefs, cid, offered, mode, conv_mode.id, provider)

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
                    meta={
                        "model": model,
                        "machine": route.machine.name,
                        **{k: v for k, v in step_stats.items() if v is not None},
                        **({"native": step.native} if step.native else {}),
                    },
                )
                # Hand the step over before the next await, so a stop during the send below
                # neither saves the text twice nor leaves its tool calls without results.
                pending_calls = list(step.calls)
                step = _Step()
                await emit({"type": "message", "message": public_message(assistant)})
                if not pending_calls:
                    break
                while pending_calls:
                    await self._run_tool(info, pending_calls[0], emit, approve)
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
        machine = route.machine.name if route else ""

        stats["duration_ms"] = round((time.monotonic() - started) * 1000)
        stats["context_tokens"] = prefs.context_tokens
        stats["machine"] = machine
        await emit(
            {
                "type": "run.end",
                "run_id": run_id,
                "conversation_id": cid,
                "stopped": stopped,
                "stats": stats,
            }
        )
        if is_new and prefs.smart_titles and model and route and not stopped:
            task = asyncio.create_task(self._smart_title(cid, first_text, route, emit))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _setup(
        self, route: Route, prefs: Any, tools: list[Tool], emit: Emit
    ) -> tuple[ToolMode, bool | None]:
        """Tool-calling mode and reasoning switch for the model on ``route``; announces it."""
        caps = await route.provider.capabilities(route.model)
        mode: ToolMode = "off"
        if tools:
            if prefs.tool_mode in ("native", "prompt"):
                mode = prefs.tool_mode
            elif caps.tools is False or route.model in self.rt.prompt_mode_models:
                mode = "prompt"
            else:
                mode = "native"
        think = prefs.think if caps.thinking else None
        await emit({"type": "model", "tool_mode": mode, **route.describe()})
        return mode, think

    async def _failover(
        self, route: Route, prefs: Any, purpose: str, tried: set[str]
    ) -> Route | None:
        self.rt.router.mark_down(route.machine)
        try:
            return await self.rt.router.choose(prefs, purpose, exclude=tried)  # type: ignore[arg-type]
        except LLMError:
            return None

    # Context --------------------------------------------------------------------------------

    def _context(
        self,
        prefs: Any,
        cid: str,
        tools: list[Tool],
        mode: ToolMode,
        conv_mode: str = "",
        provider: Provider | None = None,
    ) -> list[dict[str, Any]]:
        rt = self.rt
        system = system_prompt(
            prefs,
            tools=tools if mode != "off" else [],
            memories=rt.store.list_memories(),
            workspace=str(rt.config.workspace),
            prompt_mode=mode == "prompt",
            knowledge=rt.knowledge.status(),
            mode=conv_mode,
        )
        stored = rt.store.list_messages(cid)
        last_user = next((m for m in reversed(stored) if m["role"] == "user"), None)
        images = self._load_images((last_user or {}).get("meta", {}).get("images") or [])
        # This turn's replies go back in the provider's own format (Claude's thinking blocks).
        # Earlier turns don't: their system prompt had another time, so they would be dropped.
        turn = max((i for i, m in enumerate(stored) if m["role"] == "user"), default=len(stored))
        for m in stored[turn:]:
            if m["role"] == "assistant" and m["meta"].get("native"):
                m["native"] = m["meta"]["native"]
        context = max(prefs.context_tokens, provider.history_tokens if provider else 0)
        reserve = min(2048, context // 4)
        specs = len(json.dumps([t.spec() for t in tools])) // 3 if mode == "native" else 0
        budget = context - reserve - len(system) // 3 - specs
        budget = max(512, budget - IMAGE_TOKENS * len(images))
        history = fit_history(stored, budget)
        if mode != "native":
            history = to_prompt_mode(history)
        if images:
            # Only the current turn's images go to the model; older ones would fill the window.
            for message in reversed(history):
                if message["role"] == "user":
                    message["images"] = images
                    break
        return [{"role": "system", "content": system}, *history]

    def _load_images(self, names: list[str]) -> list[str]:
        out = []
        for name in names[:4]:
            if not CAPTURE_NAME.match(name):
                continue
            path = self.rt.captures_dir / name
            try:
                out.append(base64.b64encode(path.read_bytes()).decode())
            except OSError:
                log.warning("Capture %s is missing", name)
        return out

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
            if chunk.native:
                step.native = chunk.native
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

    async def _run_tool(self, info: _RunInfo, call: ToolCall, emit: Emit, approve: Approve) -> None:
        prefs, cid, policy = info.prefs, info.cid, info.policy
        blocked = set(denied(prefs))
        tool = self.rt.registry.get(call.name) or self._offered.get(call.name)
        if tool and tool.name in blocked:
            tool = None
        permission = permission_for(tool, prefs) if tool else "deny"
        await emit(
            {
                "type": "tool.start",
                "call": {"id": call.id, "name": call.name, "arguments": call.arguments},
                "summary": tool.summary if tool else "",
                "risk": tool.risk if tool else "safe",
                "permission": permission,
                "category": tool.category if tool else "general",
            }
        )
        started = time.monotonic()
        ok = False
        ran = False
        ui: dict[str, Any] = {}
        decision = "unknown"
        if tool is None:
            names = ", ".join(t.name for t in self.rt.registry.enabled(blocked))
            result = f"Error: there is no tool named '{call.name}'. Available tools: {names}"
        elif policy and (reason := policy.check(tool, call.arguments)):
            result = f"Not run: {reason}"
            decision = "blocked"
        else:
            allowed = True
            decision = "auto"
            if permission == "ask":
                await emit({"type": "status", "state": "approval"})
                await emit(
                    {
                        "type": "approval.request",
                        "call": {"id": call.id, "name": call.name, "arguments": call.arguments},
                        "description": tool.description,
                    }
                )
                allowed = await approve(call, tool)
                decision = "approved" if allowed else "denied"
                await emit({"type": "approval.result", "id": call.id, "allowed": allowed})
            if not allowed:
                result = "The user declined this action. Don't retry it unless they ask."
            else:
                await emit({"type": "status", "state": "tool", "tool": call.name})
                started = time.monotonic()
                ran = True
                try:
                    result, ui = await tool.run(call.arguments, self.rt.tool_context(cid))
                    ok = True
                    if policy:
                        policy.observe(tool, result)
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
            meta={"ok": ok, "duration_ms": duration, **({"ui": ui} if ui else {})},
        )
        try:
            self.rt.store.add_audit(
                conversation_id=cid,
                source=info.source,
                tool=call.name,
                arguments=call.arguments,
                permission=permission,
                decision=decision,
                ok=ok if ran else None,
                duration_ms=duration if ran else None,
                sandboxed=bool(ui.get("sandboxed")),
                detail=None if ok else result[:300],
            )
        except Exception:  # The audit log must never break a run.
            log.exception("Could not write the audit log")
        await emit(
            {
                "type": "tool.end",
                "id": call.id,
                "ok": ok,
                "result": result[:RESULT_PREVIEW_CHARS],
                "duration_ms": duration,
                "ui": ui,
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

    async def _smart_title(self, cid: str, text: str, route: Route, emit: Emit) -> None:
        try:
            prefs, _ = self.rt.preferences()
            provider, model = route.provider, route.model
            if prefs.light_local and not route.machine.is_local:
                try:  # Titles are light work; keep the GPU machine for answers.
                    light = await self.rt.router.choose(prefs, "light")
                    provider, model = light.provider, light.model
                except LLMError:
                    pass
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
