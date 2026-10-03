"""HTTP API, WebSocket chat endpoint and static web UI."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from bagley import __version__
from bagley.agent import Agent, RunRequest
from bagley.config import LOOPBACK_HOSTS, SECRET_PREFERENCES, ServerConfig
from bagley.llm import LLMError, ToolCall
from bagley.prompts import PERSONAS
from bagley.runtime import Runtime
from bagley.tools import Tool

log = logging.getLogger("bagley.server")

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_COOKIE = "bagley_token"
APPROVAL_TIMEOUT = 600.0


# Security -------------------------------------------------------------------------------------


class GuardMiddleware:
    """Blocks DNS rebinding (Host check), cross-site requests (Origin check) and, when a token is
    configured, unauthenticated access."""

    def __init__(self, app: ASGIApp, config: ServerConfig) -> None:
        self.app = app
        self.config = config
        self.hosts = {h.lower() for h in (*LOOPBACK_HOSTS, *config.allowed_hosts)}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        hostname = urlsplit(f"//{host}").hostname or ""
        if self.config.is_loopback and hostname.lower() not in self.hosts:
            await self._deny(scope, receive, send, 400, "Invalid Host header.")
            return
        origin = headers.get("origin")
        unsafe = scope["type"] == "websocket" or scope.get("method") not in (
            "GET",
            "HEAD",
            "OPTIONS",
        )
        if origin and unsafe and urlsplit(origin).netloc.lower() != host.lower():
            await self._deny(scope, receive, send, 403, "Cross-origin request blocked.")
            return
        if self.config.token and not self._authorized(scope, headers):
            query = parse_qs(scope.get("query_string", b"").decode())
            supplied = (query.get("token") or [""])[0]
            if (
                scope["type"] == "http"
                and supplied
                and hmac.compare_digest(supplied, self.config.token)
            ):
                await self._set_cookie_redirect(send, scope.get("path", "/"))
                return
            await self._deny(
                scope,
                receive,
                send,
                401,
                "Unauthorized. Open the URL printed by `bagley` (it includes ?token=...).",
            )
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope: Scope, headers: dict[str, str]) -> bool:
        token = self.config.token
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], token):
            return True
        cookie = SimpleCookie()
        with contextlib.suppress(Exception):
            cookie.load(headers.get("cookie", ""))
        morsel = cookie.get(TOKEN_COOKIE)
        return bool(morsel and hmac.compare_digest(morsel.value, token))

    async def _set_cookie_redirect(self, send: Send, path: str) -> None:
        cookie = f"{TOKEN_COOKIE}={self.config.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [(b"location", path.encode()), (b"set-cookie", cookie.encode())],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008, "reason": message})
            return
        await PlainTextResponse(message, status_code=status)(scope, receive, send)


# Chat sessions --------------------------------------------------------------------------------


class ChatSession:
    """One WebSocket connection. Runs one agent turn at a time and brokers approvals."""

    def __init__(self, ws: WebSocket, runtime: Runtime) -> None:
        self.ws = ws
        self.agent = Agent(runtime)
        self.task: asyncio.Task[None] | None = None
        self.approvals: dict[str, asyncio.Future[str]] = {}
        self.always_allow: set[str] = set()
        self._send_lock = asyncio.Lock()

    async def emit(self, event: dict[str, Any]) -> None:
        async with self._send_lock:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await self.ws.send_text(json.dumps(event, default=str))

    async def approve(self, call: ToolCall, tool: Tool) -> bool:
        if tool.name in self.always_allow:
            return True
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.approvals[call.id] = fut
        try:
            decision = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            self.approvals.pop(call.id, None)
        if decision == "always":
            self.always_allow.add(tool.name)
        return decision in ("allow", "always")

    async def serve(self) -> None:
        try:
            while True:
                try:
                    data = json.loads(await self.ws.receive_text())
                except (json.JSONDecodeError, TypeError):
                    await self.emit({"type": "error", "message": "Malformed message."})
                    continue
                await self.handle(data)
        except WebSocketDisconnect:
            pass
        finally:
            if self.task and not self.task.done():
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.task

    async def handle(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "chat":
            if self.task and not self.task.done():
                await self.emit({"type": "error", "message": "Still working on the last message."})
                return
            mode = data.get("mode", "send")
            if mode not in ("send", "regenerate", "edit"):
                mode = "send"
            req = RunRequest(
                text=str(data.get("text", ""))[:100_000],
                conversation_id=data.get("conversation_id") or None,
                mode=mode,
            )
            self.task = asyncio.create_task(self.agent.run(req, self.emit, self.approve))
        elif kind == "cancel":
            for fut in self.approvals.values():
                if not fut.done():
                    fut.set_result("deny")
            if self.task and not self.task.done():
                self.task.cancel()
        elif kind == "approval":
            fut = self.approvals.get(str(data.get("id")))
            if fut and not fut.done():
                fut.set_result(str(data.get("decision", "deny")))
        elif kind == "ping":
            await self.emit({"type": "pong"})


# App ------------------------------------------------------------------------------------------


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MemoryBody(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class PullBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _public_preferences(runtime: Runtime) -> dict[str, Any]:
    prefs, locked = runtime.preferences()
    values = prefs.model_dump()
    for key in SECRET_PREFERENCES:
        values[f"has_{key}"] = bool(values.pop(key))
    return {
        "values": values,
        "locked": sorted(locked),
        "personas": {
            k: {"label": v["label"], "description": v["description"]} for k, v in PERSONAS.items()
        },
    }


def _export_markdown(conv: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    lines = [
        f"# {conv['title']}",
        "",
        f"_Exported from Bagley on {datetime.now():%Y-%m-%d %H:%M}_",
        "",
    ]
    for m in messages:
        when = datetime.fromtimestamp(m["created_at"]).strftime("%Y-%m-%d %H:%M")
        if m["role"] == "user":
            lines += [f"**You** · {when}", "", m["content"], ""]
        elif m["role"] == "assistant":
            if m["content"]:
                lines += [f"**Bagley** · {when}", "", m["content"], ""]
            for call in m.get("tool_calls") or []:
                fn = call["function"]
                lines += [f"> Used `{fn['name']}` with `{fn['arguments']}`", ""]
    return "\n".join(lines).rstrip() + "\n"


def create_app(runtime: Runtime | None = None, config: ServerConfig | None = None) -> FastAPI:
    config = runtime.config if runtime else (config or ServerConfig.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime or Runtime(config)
        app.state.runtime = rt
        await rt.start()
        try:
            yield
        finally:
            await rt.aclose()

    app = FastAPI(
        title="Bagley", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.add_middleware(GuardMiddleware, config=config)

    def rt() -> Runtime:
        return app.state.runtime

    index_html = (
        (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{version}}", __version__)
    )

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        info = await rt().health()
        info["version"] = __version__
        return info

    @app.get("/api/info")
    async def info() -> dict[str, Any]:
        r = rt()
        return {
            "version": __version__,
            "workspace": str(r.config.workspace),
            "data_dir": str(r.config.data_dir),
            "shell_enabled": r.config.enable_shell,
            "search": "searxng" if r.config.searxng_url else "duckduckgo",
        }

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        try:
            provider = await rt().provider()
            items = await provider.list_models()
        except LLMError as exc:
            return JSONResponse({"error": exc.message, "hint": exc.hint}, status_code=502)
        return {
            "provider": provider.kind,
            "can_pull": provider.supports_pull,
            "models": [m.to_dict() for m in items],
        }

    @app.post("/api/models/pull")
    async def pull_model(body: PullBody) -> StreamingResponse:
        provider = await rt().provider()
        if not provider.supports_pull:
            raise HTTPException(400, "This model server can't download models.")

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for event in provider.pull(body.name.strip()):
                    yield (json.dumps(event) + "\n").encode()
            except LLMError as exc:
                yield (json.dumps({"error": exc.message}) + "\n").encode()

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.get("/api/preferences")
    async def get_preferences() -> dict[str, Any]:
        return _public_preferences(rt())

    @app.put("/api/preferences")
    async def put_preferences(changes: dict[str, Any]) -> dict[str, Any]:
        try:
            rt().update_preferences(changes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _public_preferences(rt())

    @app.get("/api/conversations")
    async def list_conversations(q: str = "") -> list[dict[str, Any]]:
        return rt().store.list_conversations(q.strip())

    @app.get("/api/conversations/{cid}")
    async def get_conversation(cid: str) -> dict[str, Any]:
        conv = rt().store.get_conversation(cid)
        if not conv:
            raise HTTPException(404, "Conversation not found.")
        return {"conversation": conv, "messages": rt().store.list_messages(cid)}

    @app.patch("/api/conversations/{cid}")
    async def rename_conversation(cid: str, body: RenameBody) -> dict[str, Any]:
        if not rt().store.rename_conversation(cid, body.title):
            raise HTTPException(404, "Conversation not found.")
        return rt().store.get_conversation(cid) or {}

    @app.delete("/api/conversations/{cid}", status_code=204)
    async def delete_conversation(cid: str) -> Response:
        if not rt().store.delete_conversation(cid):
            raise HTTPException(404, "Conversation not found.")
        return Response(status_code=204)

    @app.post("/api/conversations/{cid}/restore")
    async def restore_conversation(cid: str) -> dict[str, Any]:
        if not rt().store.restore_conversation(cid):
            raise HTTPException(404, "Conversation not found.")
        return rt().store.get_conversation(cid) or {}

    @app.get("/api/conversations/{cid}/export")
    async def export_conversation(cid: str) -> Response:
        conv = rt().store.get_conversation(cid)
        if not conv:
            raise HTTPException(404, "Conversation not found.")
        body = _export_markdown(conv, rt().store.list_messages(cid))
        safe = (
            "".join(c if c.isalnum() or c in " -_" else "_" for c in conv["title"]).strip()
            or "chat"
        )
        return Response(
            body,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe[:60]}.md"'},
        )

    @app.get("/api/memories")
    async def list_memories() -> list[dict[str, Any]]:
        return rt().store.list_memories()

    @app.post("/api/memories", status_code=201)
    async def add_memory(body: MemoryBody) -> dict[str, Any]:
        return rt().store.add_memory(body.content)

    @app.delete("/api/memories/{mid}", status_code=204)
    async def delete_memory(mid: int) -> Response:
        if not rt().store.delete_memory(mid):
            raise HTTPException(404, "Memory not found.")
        return Response(status_code=204)

    @app.get("/api/tools")
    async def list_tools() -> dict[str, Any]:
        r = rt()
        prefs, _ = r.preferences()
        disabled = set(prefs.disabled_tools)
        return {
            "tools": [
                {**t.describe(), "enabled": t.name not in disabled}
                for t in r.registry.tools.values()
            ],
            "errors": r.registry.errors,
            "mcp": r.mcp.status(),
            "shell_enabled": r.config.enable_shell,
        }

    @app.websocket("/api/ws")
    async def chat_socket(ws: WebSocket) -> None:
        await ws.accept()
        await ChatSession(ws, rt()).serve()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
