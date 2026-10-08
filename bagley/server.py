"""HTTP API, WebSocket chat endpoint and static web UI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from bagley import __version__, automations, journal
from bagley.agent import Agent, RunRequest
from bagley.api import routers
from bagley.approvals import Pending, summarize
from bagley.config import (
    LOOPBACK_HOSTS,
    PREFERENCE_ENV,
    SECRET_PREFERENCES,
    ServerConfig,
    env_preferences,
)
from bagley.llm import LLMError, ToolCall
from bagley.modes import MODES, public_modes
from bagley.permissions import default_permission, permission_for
from bagley.prompts import PERSONAS
from bagley.runtime import Runtime
from bagley.tools import Tool

log = logging.getLogger("bagley.server")

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_COOKIE = "bagley_token"
APPROVAL_TIMEOUT = 600.0
MAX_UPLOAD_BYTES = 5_000_000


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
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and self._matches(auth[7:].strip()):
            return True
        # Parse by hand: other apps on this host can set cookies SimpleCookie chokes on.
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == TOKEN_COOKIE and self._matches(value):
                return True
        return False

    def _matches(self, supplied: str) -> bool:
        return hmac.compare_digest(supplied.encode(), self.config.token.encode())

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
    """One WebSocket connection. Runs one agent turn at a time; its approvals go through the
    shared broker, so another window, the desktop or the phone can answer them too."""

    def __init__(self, ws: WebSocket, runtime: Runtime) -> None:
        self.ws = ws
        self.agent = Agent(runtime)
        self.task: asyncio.Task[None] | None = None
        self.asked: set[str] = set()  # Approval ids this window's runs are waiting on.
        self.always_allow: set[str] = set()
        self._send_lock = asyncio.Lock()
        self._conversation: str | None = None

    async def emit(self, event: dict[str, Any]) -> None:
        async with self._send_lock:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await self.ws.send_text(json.dumps(event, default=str))

    async def approve(self, call: ToolCall, tool: Tool) -> bool:
        if tool.name in self.always_allow:
            return True
        broker = self.agent.rt.approvals
        self.asked.add(call.id)
        try:
            decision = await broker.request(
                Pending(
                    id=call.id,
                    tool=tool.name,
                    arguments=call.arguments,
                    summary=summarize(tool.summary, tool.name, call.arguments),
                    conversation_id=self._conversation,
                    source="web",
                ),
                timeout=APPROVAL_TIMEOUT,
            )
        finally:
            self.asked.discard(call.id)
        if decision == "always":
            self.always_allow.add(tool.name)
        return decision in ("allow", "always")

    async def emit_run(self, event: dict[str, Any]) -> None:
        if event["type"] in ("run.start", "conversation"):
            self._conversation = event.get("conversation_id") or (
                event.get("conversation") or {}
            ).get("id")
        await self.emit(event)

    async def serve(self) -> None:
        runtime = self.agent.rt
        runtime.listeners.add(self.emit)
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
            runtime.listeners.discard(self.emit)
            runtime.approvals.deny_all(set(self.asked))
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
            images = data.get("images") if isinstance(data.get("images"), list) else []
            req = RunRequest(
                text=str(data.get("text", ""))[:100_000],
                conversation_id=data.get("conversation_id") or None,
                mode=mode,
                source="web",
                images=[str(i) for i in images[:4]],
                conversation_mode=str(data.get("conversation_mode") or "") or None,
            )
            self._conversation = req.conversation_id
            self.task = asyncio.create_task(self.agent.run(req, self.emit_run, self.approve))
        elif kind == "cancel":
            self.agent.rt.approvals.deny_all(set(self.asked))
            if self.task and not self.task.done():
                self.task.cancel()
        elif kind == "approval":
            self.agent.rt.approvals.resolve(str(data.get("id")), str(data.get("decision", "deny")))
        elif kind == "ping":
            await self.emit({"type": "pong"})


# App ------------------------------------------------------------------------------------------


class AppStatic(StaticFiles):
    """Static files that the browser revalidates on every load, so upgrades never mix old and
    new modules. Revalidation is a cheap 304 thanks to ETags."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class RenameBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    mode: str | None = Field(default=None, max_length=32)


class AutomationBody(BaseModel):
    kind: str = Field(pattern="^[a-z_]{1,24}$")  # Checked against the registered kinds.
    name: str = Field(default="", max_length=120)
    prompt: str = Field(default="", max_length=4000)
    schedule: str = Field(min_length=1, max_length=120)
    target: str | None = Field(default=None, max_length=2000)


class AutomationPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, max_length=4000)
    schedule: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None


class FolderBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


class MemoryBody(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class PullBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _public_preferences(runtime: Runtime) -> dict[str, Any]:
    prefs, locked = runtime.preferences()
    values = prefs.model_dump()
    for key in SECRET_PREFERENCES:
        values[f"has_{key}"] = bool(values.pop(key))
    for machine in values["machines"]:
        machine["has_api_key"] = bool(machine.pop("api_key"))
    from_env = env_preferences(runtime.env)
    return {
        "modes": public_modes(prefs),
        "machine": runtime.router.machines(prefs)[0].name,
        "values": values,
        "locked": sorted(locked),
        # Which variable locks each field (an env API key also pins the server it is sent to).
        "locked_by": {k: PREFERENCE_ENV[k if k in from_env else "api_key"] for k in locked},
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


def content_security_policy(html: str) -> str:
    """Only this origin may supply scripts, styles, images and connections. The inline theme
    script is pinned by hash; model output can never load remote images or frames."""
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
        for body in re.findall(r"<script>(.*?)</script>", html, re.S)
    )
    return (
        "default-src 'self'; "
        f"script-src 'self' {hashes}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


def create_app(runtime: Runtime | None = None, config: ServerConfig | None = None) -> FastAPI:
    config = runtime.config if runtime else (config or ServerConfig.from_env())
    background: set[asyncio.Task[None]] = set()  # "Run now" tasks, stopped on shutdown.

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime or Runtime(config)
        app.state.runtime = rt
        await rt.start(background=True)
        try:
            yield
        finally:
            for task in list(background):
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
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

    page_headers = {
        "Cache-Control": "no-cache",
        "Content-Security-Policy": content_security_policy(index_html),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers=page_headers)

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

    @app.get("/api/models/loaded")
    async def loaded_models() -> list[dict[str, Any]]:
        try:
            return await (await rt().provider()).loaded()
        except LLMError:
            return []

    @app.post("/api/models/unload")
    async def unload_model(body: PullBody) -> dict[str, Any]:
        provider = await rt().provider()
        if not provider.supports_pull:
            raise HTTPException(400, "This model server manages memory itself.")
        try:
            await provider.unload(body.name.strip())
        except LLMError as exc:
            raise HTTPException(502, exc.message) from exc
        return {"ok": True}

    @app.delete("/api/models/{name:path}", status_code=204)
    async def delete_model(name: str) -> Response:
        runtime = rt()
        provider = await runtime.provider()
        if not provider.supports_pull:
            raise HTTPException(400, "This model server can't delete models.")
        try:
            await provider.delete(name)
        except LLMError as exc:
            status = exc.status if exc.status and exc.status < 500 else 502
            raise HTTPException(status, exc.message) from exc
        prefs, _ = runtime.preferences()
        if name in (prefs.model, prefs.embedding_model):
            field = "model" if name == prefs.model else "embedding_model"
            runtime.update_preferences({field: ""})
        return Response(status_code=204)

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
        store = rt().store
        conv = store.get_conversation(cid)
        if not conv:
            raise HTTPException(404, "Conversation not found.")
        if conv.get("unread"):
            store.set_unread(cid, False)
            conv["unread"] = 0
        messages = store.list_messages(cid)
        uis = [m["meta"]["ui"] for m in messages if m["meta"].get("ui", {}).get("journal_id")]
        reverted = store.reverted_ids([ui["journal_id"] for ui in uis])
        for ui in uis:
            ui["reverted"] = ui["journal_id"] in reverted
        return {"conversation": conv, "messages": messages}

    @app.post("/api/journal/{jid}/revert")
    async def revert_change(jid: int) -> dict[str, Any]:
        try:
            entry = await asyncio.to_thread(journal.revert, rt().store, rt().config, jid)
        except journal.JournalError as exc:
            raise HTTPException(409, str(exc)) from exc
        return entry

    @app.patch("/api/conversations/{cid}")
    async def rename_conversation(cid: str, body: RenameBody) -> dict[str, Any]:
        store = rt().store
        if not store.get_conversation(cid):
            raise HTTPException(404, "Conversation not found.")
        if body.mode is not None:
            if body.mode not in MODES:
                raise HTTPException(422, f"Unknown mode '{body.mode}'.")
            store.set_mode(cid, body.mode)
        if body.title is not None:
            store.rename_conversation(cid, body.title)
        return store.get_conversation(cid) or {}

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
        name = "".join(c if c.isalnum() or c in " -_" else "_" for c in conv["title"]).strip()
        name = name[:60] or "chat"
        ascii_name = name.encode("ascii", "ignore").decode().strip() or "chat"
        return Response(
            body,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{ascii_name}.md"; '
                f"filename*=UTF-8''{quote(name)}.md"
            },
        )

    @app.get("/api/workspace/raw")
    async def workspace_image(path: str) -> Response:
        """Serve an image from the workspace, e.g. a chart made by run_python."""
        root = Path(rt().config.workspace or ".").resolve()
        target = (root / path.replace("\\", "/").lstrip("/")).resolve()
        types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        types |= {".gif": "image/gif", ".webp": "image/webp"}
        if root not in target.parents or target.suffix.lower() not in types:
            raise HTTPException(404, "Not found.")
        if not target.is_file():
            raise HTTPException(404, "Not found.")
        return FileResponse(
            target,
            media_type=types[target.suffix.lower()],
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.put("/api/workspace/uploads/{name}", status_code=201)
    async def upload(name: str, request: Request) -> dict[str, Any]:
        """Save a file the user attached into the workspace, where the file tools can read it."""
        clean = "".join(c if c.isalnum() or c in "._- " else "_" for c in Path(name).name).strip(
            " ."
        )
        if not clean:
            raise HTTPException(400, "Invalid file name.")
        too_big = HTTPException(
            413, f"Files up to {MAX_UPLOAD_BYTES // 1_000_000} MB can be attached."
        )
        length = request.headers.get("content-length") or "0"
        if not length.isdigit():
            raise HTTPException(400, "Invalid Content-Length.")
        if int(length) > MAX_UPLOAD_BYTES:
            raise too_big
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_UPLOAD_BYTES:
                raise too_big
        workspace = Path(rt().config.workspace or ".").resolve()
        folder = workspace / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        if folder.is_symlink() or workspace not in folder.resolve().parents:
            raise HTTPException(400, "The uploads folder must be inside the workspace.")
        stem, suffix = Path(clean).stem, Path(clean).suffix
        for n in range(1, 1000):
            target = folder / (clean if n == 1 else f"{stem}-{n}{suffix}")
            try:
                with target.open("xb") as fh:  # Exclusive create: never follows or replaces links.
                    fh.write(body)
                break
            except FileExistsError:
                continue
        else:
            raise HTTPException(409, "Too many files with that name.")
        return {"path": f"uploads/{target.name}", "size": len(body)}

    @app.get("/api/automations")
    async def list_automations() -> list[dict[str, Any]]:
        sched = rt().scheduler
        return [sched.describe(a) for a in rt().store.list_automations()]

    @app.get("/api/automations/preview")
    async def preview_schedule(schedule: str, kind: str = "task") -> dict[str, Any]:
        try:
            return automations.preview(schedule, kind)
        except automations.ScheduleError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/automations", status_code=201)
    async def create_automation(body: AutomationBody) -> dict[str, Any]:
        try:
            item = rt().scheduler.create(
                body.kind, body.name or body.prompt[:60], body.schedule, body.prompt, body.target
            )
        except automations.ScheduleError as exc:
            raise HTTPException(422, str(exc)) from exc
        await rt().broadcast({"type": "automations.changed"})
        return rt().scheduler.describe(item)

    @app.patch("/api/automations/{aid}")
    async def update_automation(aid: int, body: AutomationPatch) -> dict[str, Any]:
        try:
            item = rt().scheduler.update(aid, **body.model_dump(exclude_none=True))
        except automations.ScheduleError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not item:
            raise HTTPException(404, "Automation not found.")
        await rt().broadcast({"type": "automations.changed"})
        return rt().scheduler.describe(item)

    @app.delete("/api/automations/{aid}", status_code=204)
    async def delete_automation(aid: int) -> Response:
        if not rt().store.delete_automation(aid):
            raise HTTPException(404, "Automation not found.")
        await rt().broadcast({"type": "automations.changed"})
        return Response(status_code=204)

    @app.post("/api/automations/{aid}/run", status_code=202)
    async def run_automation(aid: int) -> dict[str, Any]:
        item = rt().store.get_automation(aid)
        if not item:
            raise HTTPException(404, "Automation not found.")
        if rt().scheduler.busy(item):
            raise HTTPException(409, "Its chat is answering right now. Try again when it's done.")
        task = asyncio.create_task(rt().scheduler.run(item))
        background.add(task)
        task.add_done_callback(background.discard)
        return {"started": True}

    @app.get("/api/knowledge")
    async def knowledge_status() -> dict[str, Any]:
        return rt().knowledge.status()

    @app.post("/api/knowledge/folders", status_code=201)
    async def add_knowledge_folder(body: FolderBody) -> dict[str, Any]:
        path = Path(body.path.strip()).expanduser()
        if not path.is_absolute():
            raise HTTPException(422, "Use a full path, e.g. ~/Documents/Notes.")
        path = path.resolve()
        if not path.is_dir():
            raise HTTPException(422, f"{path} is not a folder on this computer.")
        if path == Path(path.anchor):
            raise HTTPException(422, "Pick a folder, not a whole drive.")
        prefs, _ = rt().preferences()
        if str(path) not in prefs.knowledge_folders:
            rt().update_preferences({"knowledge_folders": [*prefs.knowledge_folders, str(path)]})
        rt().knowledge.request_reindex()
        return rt().knowledge.status()

    @app.delete("/api/knowledge/folders")
    async def remove_knowledge_folder(path: str) -> dict[str, Any]:
        prefs, _ = rt().preferences()
        remaining = [p for p in prefs.knowledge_folders if p != path]
        if len(remaining) == len(prefs.knowledge_folders):
            raise HTTPException(404, "That folder is not in the knowledge base.")
        rt().update_preferences({"knowledge_folders": remaining})
        await asyncio.to_thread(rt().knowledge.forget_folder, Path(path))
        rt().knowledge.request_reindex()  # Also drops anything a running pass re-adds.
        return rt().knowledge.status()

    @app.post("/api/knowledge/reindex", status_code=202)
    async def reindex_knowledge() -> dict[str, Any]:
        rt().knowledge.request_reindex()
        return {"started": True}

    @app.get("/api/knowledge/search")
    async def search_knowledge(q: str, limit: int = 8) -> list[dict[str, Any]]:
        return await rt().knowledge.search(q, limit)

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
        return {
            "tools": [
                {
                    **t.describe(),
                    "enabled": permission_for(t, prefs) != "deny",
                    "permission": permission_for(t, prefs),
                    "default_permission": default_permission(t),
                }
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

    for router in routers():
        app.include_router(router)

    app.mount("/static", AppStatic(directory=STATIC_DIR), name="static")
    return app
