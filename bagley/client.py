"""Talk to a running Bagley from the command line, the Quickshell overlay, zsh or scripts.

``Client`` finds the server at ``BAGLEY_URL`` (default ``http://127.0.0.1:<BAGLEY_PORT>``) and
sends ``BAGLEY_TOKEN`` when set, which is how a laptop reaches an always-on runner over
Tailscale. ``ask`` streams a turn's events. When no server is running, ``ask`` runs the turn in
this process instead, so the commands work either way.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
from collections.abc import Iterator
from typing import Any

import httpx

from bagley.config import DEFAULT_PORT


class ServerUnavailable(Exception):
    pass


def server_url(env: dict[str, str] | os._Environ[str] | None = None) -> str:
    env = os.environ if env is None else env
    if env.get("BAGLEY_URL"):
        return env["BAGLEY_URL"].rstrip("/")
    port = env.get("BAGLEY_PORT") or DEFAULT_PORT
    return f"http://127.0.0.1:{port}"


class Client:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 10.0):
        self.url = (url or server_url()).rstrip("/")
        token = token if token is not None else os.environ.get("BAGLEY_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def available(self) -> bool:
        try:
            return self.http.get("/api/activity", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            return False

    def _check(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text
            raise ServerUnavailable(f"{resp.status_code}: {detail}")
        return resp.json() if resp.content else None

    def get(self, path: str, **params: Any) -> Any:
        try:
            return self._check(self.http.get(path, params=params))
        except httpx.HTTPError as exc:
            raise ServerUnavailable(str(exc)) from exc

    def post(self, path: str, body: Any = None) -> Any:
        try:
            return self._check(self.http.post(path, json=body or {}))
        except httpx.HTTPError as exc:
            raise ServerUnavailable(str(exc)) from exc

    def stream(
        self, path: str, body: Any = None, *, method: str = "POST"
    ) -> Iterator[dict[str, Any]]:
        """NDJSON events from ``path``."""
        try:
            with self.http.stream(
                method, path, json=body if method == "POST" else None, timeout=None
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    self._check(resp)
                for line in resp.iter_lines():
                    if line.strip():
                        yield json.loads(line)
        except httpx.HTTPError as exc:
            raise ServerUnavailable(str(exc)) from exc

    def ask(self, **body: Any) -> Iterator[dict[str, Any]]:
        return self.stream("/api/ask", body)

    def follow_activity(self) -> Iterator[dict[str, Any]]:
        return self.stream("/api/activity/stream", method="GET")

    def decide(self, approval_id: str, decision: str) -> None:
        self.post(f"/api/approvals/{approval_id}", {"decision": decision})


def ask_local(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Run one turn in this process (no server) and yield its events. Approvals are denied
    unless ``body["approvals"] == "allow"``, which allows them all (``--yes``)."""
    from bagley.config import ServerConfig

    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    async def main() -> None:
        from bagley.agent import Agent, RunRequest
        from bagley.api.desktop import context_block, save_capture
        from bagley.runtime import Runtime

        rt = Runtime(ServerConfig.from_env())
        await rt.start()
        try:
            names = []
            for image in body.get("images") or []:
                import base64

                names.append(save_capture(rt, base64.b64decode(image["data"])))

            async def emit(event: dict[str, Any]) -> None:
                events.put(event)

            async def approve(call: Any, tool: Any) -> bool:
                return body.get("approvals") == "allow"

            context = body.get("context") or {}
            text = (body.get("text") or "").strip() or ("What's on my screen?" if names else "")
            request = RunRequest(
                text=text + context_block(context),
                conversation_id=body.get("conversation_id"),
                source=body.get("source", "cli"),
                images=names,
                conversation_mode=body.get("mode"),
                tools=body.get("tools", True),
                user_meta={"context": context} if context else {},
            )
            await Agent(rt).run(request, emit, approve)
        finally:
            await rt.aclose()

    def worker() -> None:
        try:
            asyncio.run(main())
        except Exception as exc:  # Surface setup failures as an error event.
            events.put({"type": "error", "message": str(exc)})
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while (event := events.get()) is not None:
        yield event
    thread.join(timeout=5)


def ask(body: dict[str, Any], *, client: Client | None = None) -> Iterator[dict[str, Any]]:
    """Ask the running server, or this process when none answers."""
    client = client or Client()
    if client.available():
        if body.get("approvals") == "allow":
            body = {**body, "approvals": "ask"}  # The server asks the user itself.
        yield from client.ask(**body)
    else:
        yield from ask_local(body)


def reply_text(events: Iterator[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Collect a turn: the final answer text and the error events."""
    text = ""
    errors: list[dict[str, Any]] = []
    for event in events:
        if event["type"] == "text.delta":
            text += event["text"]
        elif event["type"] == "text.retract":  # That text was the model thinking.
            text = text[: len(text) - int(event.get("chars") or 0)]
        elif event["type"] == "message" and text and not text.endswith("\n"):
            text += "\n\n"
        elif event["type"] == "error":
            errors.append(event)
    return text.strip(), errors
