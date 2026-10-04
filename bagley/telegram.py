"""Telegram gateway: talk to Bagley from your phone.

The bot polls Telegram for messages (outbound HTTPS only, so nothing on your network has to be
exposed). Only chats paired with a one-time code from Settings are served. Approvals and
ask_user questions arrive as buttons, charts as photos, and automation results and reminders
as messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from bagley.llm import ToolCall
from bagley.tools import Tool

if TYPE_CHECKING:
    from bagley.runtime import Runtime

log = logging.getLogger("bagley.telegram")

API = "https://api.telegram.org"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_TTL = 15 * 60
MAX_ATTEMPTS = 5  # Wrong pairing codes per chat before it is ignored for LOCKOUT seconds.
LOCKOUT = 3600
CODE_LENGTH = 8
WAIT = 600  # Seconds an approval or question waits for a tap.
LIMIT = 4000  # Telegram allows 4096 characters per message.
HELP = (
    "Talk to me like in the app. /new starts a new chat, /stop cancels the current reply, "
    "/help shows this."
)


class TelegramError(Exception):
    pass


def to_html(text: str) -> str:
    """The Markdown subset models write, as Telegram HTML."""
    parts = re.split(r"(```[\s\S]*?```)", text)
    out = []
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            body = re.sub(r"^```[\w+-]*\n?", "", part[:-3])
            out.append(f"<pre>{html.escape(body.rstrip(), quote=False)}</pre>")
            continue
        s = html.escape(part, quote=False)
        s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", s)
        out.append(s)
    return "".join(out).strip()


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Split on paragraph or line breaks so each piece fits in one message."""
    pieces, rest = [], text.strip()
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


class TelegramGateway:
    def __init__(self, runtime: Runtime, transport: httpx.AsyncBaseTransport | None = None):
        self.rt = runtime
        self.http = httpx.AsyncClient(base_url=API, transport=transport, timeout=70.0)
        self.state = "off"  # off | connecting | ok | error
        self.error = ""
        self.bot = ""
        self.code = ""
        self.code_expires = 0.0
        self.attempts: dict[int, tuple[int, float]] = {}  # chat -> (wrong codes, first one)
        self.runs: dict[int, asyncio.Task[None]] = {}
        self.pending: dict[str, asyncio.Future[str]] = {}  # Button taps by key.
        self.replies: dict[int, asyncio.Future[str]] = {}  # Typed answers to ask_user.
        self._task: asyncio.Task[None] | None = None
        self._offset = 0
        self._token = ""
        self.version = 0  # Lets clients drop a status older than one they already have.

    # Lifecycle ----------------------------------------------------------------------------

    def token(self) -> str:
        return self.rt.preferences()[0].telegram_token.strip()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        token = self.token()
        if token != self._token:
            self._offset = 0  # Update ids are per bot; a new bot starts its own count.
        self._token = token
        if not self._token:
            self.state, self.error, self.bot = "off", "", ""
            return
        self.state = "connecting"
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in [self._task, *self.runs.values()]:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._task = None
        self.runs.clear()
        self.state = "off"

    async def restart(self) -> None:
        await self.stop()
        self.start()
        await self.changed()

    async def aclose(self) -> None:
        await self.stop()
        await self.http.aclose()

    # Telegram API -------------------------------------------------------------------------

    async def call(self, method: str, http_timeout: float = 20.0, **params: Any) -> Any:
        try:
            resp = await self.http.post(
                f"/bot{self._token}/{method}",
                json={k: v for k, v in params.items() if v is not None},
                timeout=http_timeout,
            )
        except httpx.HTTPError as exc:
            raise TelegramError(f"Can't reach Telegram: {exc.__class__.__name__}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise TelegramError(f"Telegram returned HTTP {resp.status_code}") from exc
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {resp.status_code}")
        return data.get("result")

    async def send(self, chat: int, text: str, buttons: list[list[dict[str, str]]] | None = None):
        """Send Markdown text as HTML, falling back to plain text if Telegram rejects the HTML."""
        sent = None
        parts = chunks(text) or ["…"]
        for i, part in enumerate(parts):
            markup = {"inline_keyboard": buttons} if buttons and i == len(parts) - 1 else None
            try:
                sent = await self.call(
                    "sendMessage",
                    chat_id=chat,
                    text=to_html(part),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
            except TelegramError as exc:
                if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
                    raise
                sent = await self.call("sendMessage", chat_id=chat, text=part, reply_markup=markup)
        return sent

    async def send_photo(self, chat: int, path: Path, caption: str = "") -> None:
        try:
            resp = await self.http.post(
                f"/bot{self._token}/sendPhoto",
                data={"chat_id": str(chat), "caption": caption[:1000]},
                files={"photo": (path.name, path.read_bytes())},
                timeout=60.0,
            )
            if not resp.json().get("ok"):
                log.warning("Telegram rejected a photo: %s", resp.text[:200])
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("Couldn't send a photo to Telegram: %s", exc)

    # Polling ------------------------------------------------------------------------------

    async def _loop(self) -> None:
        delay = 5.0
        while True:
            try:
                if self.state != "ok":
                    me = await self.call("getMe")
                    self.bot, self.state, self.error = me.get("username", ""), "ok", ""
                    self.new_code()
                    await self.changed()
                updates = await self.call(
                    "getUpdates",
                    http_timeout=65.0,
                    offset=self._offset or None,
                    timeout=50,
                    allowed_updates=["message", "callback_query"],
                )
                delay = 5.0
                for update in updates or []:
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    try:
                        await self.handle(update)
                    except Exception:
                        log.exception("Telegram update failed")
            except asyncio.CancelledError:
                raise
            except TelegramError as exc:
                fatal = any(w in str(exc).lower() for w in ("unauthorized", "not found"))
                self.state, self.error = "error", str(exc)
                await self.changed()
                if fatal:
                    return  # A bad token won't fix itself; Settings restarts the gateway.
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            except Exception as exc:
                log.exception("Telegram polling failed")
                self.state, self.error = "error", str(exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._tap(update["callback_query"])
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if chat.get("type") != "private" or not text:
            return
        cid = int(chat["id"])
        if not self.paired(cid):
            await self._pair(cid, msg, text)
            return
        if cid in self.replies and not self.replies[cid].done() and not text.startswith("/"):
            self.replies[cid].set_result(text)
            return
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/help"):
            await self.send(cid, HELP)
        elif command == "/new":
            self._set_chat(cid, conversation_id=None)
            await self.send(cid, "New chat started.")
        elif command == "/stop":
            task = self.runs.get(cid)
            if task and not task.done():
                task.cancel()
                await self.send(cid, "Stopped.")
            else:
                await self.send(cid, "Nothing is running.")
        elif command == "/pair":
            await self.send(cid, "This chat is already paired.")
        elif self.runs.get(cid) and not self.runs[cid].done():
            await self.send(cid, "Still working on your last message. Send /stop to cancel it.")
        else:
            self.runs[cid] = asyncio.create_task(self._run(cid, text))

    # Pairing ------------------------------------------------------------------------------

    def chats(self) -> list[dict[str, Any]]:
        return list(self.rt.preferences()[0].telegram_chats)

    def paired(self, chat: int) -> bool:
        return any(c.get("id") == chat for c in self.chats())

    def new_code(self) -> str:
        self.version += 1
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self.code, self.code_expires = f"{raw[:4]}-{raw[4:]}", time.time() + CODE_TTL
        return self.code

    def current_code(self) -> str:
        if not self.code or time.time() > self.code_expires:
            self.new_code()
        return self.code

    async def _pair(self, chat: int, msg: dict[str, Any], text: str) -> None:
        # "/pair CODE", "/start CODE" (from the t.me link in Settings) or just the code.
        upper = text.upper()
        given = re.sub(r"[^A-Z0-9]", "", upper.removeprefix("/PAIR").removeprefix("/START"))
        wrong, since = self.attempts.get(chat, (0, 0.0))
        if wrong >= MAX_ATTEMPTS and time.time() - since < LOCKOUT:
            return  # Too many wrong codes from this chat; stay silent for a while.
        expected = re.sub(r"[^A-Z0-9]", "", self.code)
        ok = bool(expected) and time.time() < self.code_expires
        if ok and secrets.compare_digest(given, expected):
            sender = msg.get("from") or {}
            name = sender.get("first_name") or sender.get("username") or str(chat)
            entry = {"id": chat, "name": str(name)[:60], "conversation_id": None}
            self.rt.update_preferences({"telegram_chats": [*self.chats(), entry]})
            self.new_code()  # Each code pairs one chat.
            await self.changed()
            await self.send(chat, f"Paired. Hi {name}, I'm Bagley. {HELP}")
            return
        if len(given) == CODE_LENGTH or upper.startswith("/PAIR"):
            # Every guess shaped like a code counts, with or without /pair.
            if wrong >= MAX_ATTEMPTS:
                wrong = 0  # The lockout has passed.
            self.attempts[chat] = (wrong + 1, since if wrong else time.time())
            await self.send(chat, "That code didn't match. Check Settings → Telegram.")
        else:
            await self.send(
                chat,
                "This Bagley isn't paired with you yet. Open Settings → Telegram on the computer "
                "running Bagley and send the pairing code shown there, like /pair ABCD-EFGH.",
            )

    def unpair(self, chat: int) -> bool:
        chats = self.chats()
        keep = [c for c in chats if c.get("id") != chat]
        if len(keep) == len(chats):
            return False
        self.rt.update_preferences({"telegram_chats": keep})
        task = self.runs.pop(chat, None)
        if task:
            task.cancel()
        return True

    def _set_chat(self, chat: int, **changes: Any) -> None:
        chats = [{**c, **changes} if c.get("id") == chat else c for c in self.chats()]
        self.rt.update_preferences({"telegram_chats": chats})

    # Running the agent ----------------------------------------------------------------------

    async def _run(self, chat: int, text: str) -> None:
        from bagley.agent import Agent, RunRequest  # Late import: the agent imports the runtime.

        entry = next(c for c in self.chats() if c.get("id") == chat)
        conversation = entry.get("conversation_id")
        if conversation and not self.rt.store.get_conversation(conversation):
            conversation = None
        status: dict[str, Any] = {"message": None, "lines": [], "answer": ""}
        root = Path(self.rt.config.workspace).resolve()

        async def typing() -> None:
            while True:
                with contextlib.suppress(TelegramError):
                    await self.call("sendChatAction", chat_id=chat, action="typing")
                await asyncio.sleep(4.5)

        async def emit(event: dict[str, Any]) -> None:
            kind = event["type"]
            if kind == "conversation":
                self._set_chat(chat, conversation_id=event["conversation"]["id"])
            elif kind == "message":
                status["answer"] = event["message"].get("content") or status["answer"]
            elif kind == "tool.start":
                status["lines"].append(f"⚙ {_summary(event)}")
                body = "\n".join(status["lines"][-8:])
                with contextlib.suppress(TelegramError):
                    if status["message"] is None:
                        status["message"] = await self.call(
                            "sendMessage", chat_id=chat, text=body, disable_notification=True
                        )
                    else:
                        await self.call(
                            "editMessageText",
                            chat_id=chat,
                            message_id=status["message"]["message_id"],
                            text=body,
                        )
            elif kind == "tool.end":
                for image in (event.get("ui") or {}).get("images", [])[:4]:
                    path = (root / image["path"]).resolve()
                    if root in path.parents and path.is_file():
                        await self.send_photo(chat, path, image["path"])
            elif kind == "error":
                hint = f"\n{event['hint']}" if event.get("hint") else ""
                await self.send(chat, f"⚠ {event['message']}{hint}")

        async def approve(call: ToolCall, tool: Tool) -> bool:
            # Show the arguments in full: what is approved is exactly what is shown.
            args = json.dumps(call.arguments, ensure_ascii=False, indent=1)
            if len(args) > 3000:
                await self.send(
                    chat,
                    f"Declined **{tool.name}**: its {len(args):,} characters of arguments are too "
                    "long to review here. Ask again from the app to see them in full.",
                )
                return False
            answer = await self._buttons(
                chat,
                f"Allow **{tool.name}**?\n```\n{args}\n```",
                [("Allow", "allow"), ("Deny", "deny")],
            )
            return answer == "allow"

        async def ask(question: str, options: list[str]) -> str | None:
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self.replies[chat] = fut
            try:
                if options:
                    tap = asyncio.ensure_future(
                        self._buttons(chat, question, [(o, o) for o in options])
                    )
                    done, _ = await asyncio.wait(
                        {fut, tap}, timeout=WAIT, return_when=asyncio.FIRST_COMPLETED
                    )
                    tap.cancel()
                    return next((d.result() for d in done if not d.cancelled()), None)
                await self.send(chat, f"{question}\n\n_Reply with your answer._")
                return await asyncio.wait_for(fut, timeout=WAIT)
            except asyncio.TimeoutError:
                return None
            finally:
                self.replies.pop(chat, None)

        pinger = asyncio.create_task(typing())
        try:
            await Agent(self.rt).run(
                RunRequest(text=text, conversation_id=conversation, ask=ask), emit, approve
            )
        finally:
            pinger.cancel()
        if status["answer"]:
            await self.send(chat, status["answer"])

    async def _buttons(self, chat: int, text: str, options: list[tuple[str, str]]) -> str | None:
        """Send a message with buttons and wait for a tap. Returns the tapped value."""
        key = secrets.token_hex(4)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        rows = [
            [{"text": label[:60], "callback_data": f"{key}:{i}"}]
            for i, (label, _) in enumerate(options)
        ]
        if len(options) <= 3 and all(len(label) <= 14 for label, _ in options):
            rows = [[cell for row in rows for cell in row]]
        try:
            sent = await self.send(chat, text, rows)
            try:
                index = await asyncio.wait_for(fut, timeout=WAIT)
            except asyncio.TimeoutError:
                return None
            label, value = options[int(index)]
            with contextlib.suppress(TelegramError):
                await self.call(
                    "editMessageText",
                    chat_id=chat,
                    message_id=sent["message_id"],
                    text=f"{_plain(text)}\n→ {label}",
                )
            return value
        finally:
            self.pending.pop(key, None)

    async def _tap(self, query: dict[str, Any]) -> None:
        chat = ((query.get("message") or {}).get("chat") or {}).get("id")
        key, _, index = str(query.get("data", "")).partition(":")
        fut = self.pending.get(key)
        with contextlib.suppress(TelegramError):
            await self.call("answerCallbackQuery", callback_query_id=query.get("id"))
        if chat is None or not self.paired(int(chat)) or not fut or fut.done():
            return
        if index.isdigit():
            fut.set_result(index)

    # Notifications ----------------------------------------------------------------------------

    async def notify(self, title: str, body: str, conversation_id: str | None) -> None:
        """Forward automation results and reminders to paired chats."""
        prefs = self.rt.preferences()[0]
        if self.state != "ok" or not prefs.telegram_notify or not conversation_id:
            return
        for c in self.chats():
            try:
                await self.send(int(c["id"]), f"**{title}**\n{body}" if body else f"**{title}**")
            except Exception as exc:  # Never let a phone notification break the caller.
                log.warning("Couldn't notify Telegram: %s", exc)

    # Status -----------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        prefs, locked = self.rt.preferences()
        return {
            "configured": bool(prefs.telegram_token),
            "locked": "telegram_token" in locked,
            "state": self.state,
            "error": self.error,
            "bot": self.bot,
            "code": self.current_code() if self.state == "ok" else "",
            "chats": [{"id": c["id"], "name": c.get("name", "")} for c in self.chats()],
            "notify": prefs.telegram_notify,
            "version": self.version,
        }

    async def changed(self) -> None:
        self.version += 1
        await self.rt.broadcast({"type": "telegram.changed", "status": self.status()})


def _summary(event: dict[str, Any]) -> str:
    """The tool card's summary text, filled from the call's arguments."""
    template, args = event.get("summary") or "", event["call"]["arguments"]

    def fill(m: re.Match[str]) -> str:
        value = args.get(m.group(1), "")
        text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        return text[:60]

    return re.sub(r"\{(\w+)\}", fill, template) or event["call"]["name"]


def _plain(text: str) -> str:
    return re.sub(r"[*_`]", "", text)
