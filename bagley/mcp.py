"""Minimal Model Context Protocol client (stdio transport, tools only).

Servers are configured in ``~/.bagley/mcp.json`` using the same shape as other MCP clients::

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"],
          "trust": false
        }
      }
    }

Tools appear as ``<server>__<tool>``. Unless ``"trust": true`` is set, each call asks the user.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
from collections import deque
from pathlib import Path
from typing import Any

from bagley import __version__
from bagley.tools import Registry, Tool, ToolError

log = logging.getLogger("bagley.mcp")

PROTOCOL_VERSION = "2025-06-18"
START_TIMEOUT = 30.0


class McpError(Exception):
    pass


class McpServer:
    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.command = str(spec.get("command", ""))
        self.args = [str(a) for a in spec.get("args", [])]
        self.env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        self.trust = bool(spec.get("trust", False))
        self.tools: list[dict[str, Any]] = []
        self.state = "stopped"
        self.error = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._stderr: deque[str] = deque(maxlen=20)

    async def start(self) -> None:
        exe = shutil.which(self.command) or self.command
        self.state = "starting"
        self._proc = await asyncio.create_subprocess_exec(
            exe,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **self.env},
            limit=16 * 1024 * 1024,
        )
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "bagley", "version": __version__},
            },
            timeout=START_TIMEOUT,
        )
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        cursor = None
        self.tools = []
        while True:
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            self.tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.state = "running"

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._proc.kill()
        self.state = "stopped"

    async def _send(self, message: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin or self._proc.returncode is not None:
            raise McpError(f"MCP server '{self.name}' is not running.")
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], timeout: float = 120.0) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise McpError(f"MCP server '{self.name}' did not answer {method} in time.") from exc
        finally:
            self._pending.pop(rid, None)

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while line := await self._proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        except (ValueError, OSError) as exc:  # Line over the size limit, or a broken pipe.
            self.error = f"Stopped reading from the server: {exc}"
            if self._proc.returncode is None:
                self._proc.kill()
        self.state = "exited"
        detail = self._stderr[-1] if self._stderr else ""
        self.error = self.error or f"Process exited. {detail}".strip()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    McpError(f"MCP server '{self.name}' stopped. {self.error}".strip())
                )

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:  # Request from the server.
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
            if msg["method"] == "ping":
                reply["result"] = {}
            else:
                reply["error"] = {"code": -32601, "message": "Method not supported"}
            with contextlib.suppress(McpError, OSError):
                await self._send(reply)
            return
        rid = msg.get("id")
        fut = self._pending.get(rid) if isinstance(rid, int) else None
        if not fut or fut.done():
            return
        if "error" in msg:
            err = msg["error"]
            fut.set_exception(
                McpError(str(err.get("message", err) if isinstance(err, dict) else err))
            )
        else:
            result = msg.get("result", {})
            fut.set_result(result if isinstance(result, dict) else {"content": []})

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while line := await self._proc.stderr.readline():
            text = line.decode("utf-8", "replace").rstrip()
            self._stderr.append(text)
            log.debug("[%s] %s", self.name, text)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            result = await self.request("tools/call", {"name": name, "arguments": arguments})
        except McpError as exc:
            raise ToolError(str(exc)) from exc
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "resource":
                parts.append(str(item.get("resource", {}).get("text", "[resource]")))
            else:
                parts.append(f"[{item.get('type', 'content')}]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"]))
        text = "\n".join(parts) or "Done."
        if result.get("isError"):
            raise ToolError(text)
        return text


def _tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]


class McpManager:
    def __init__(self, config_path: Path | None, registry: Registry) -> None:
        self.config_path = config_path
        self.registry = registry
        self.servers: dict[str, McpServer] = {}

    def _load(self) -> dict[str, Any]:
        if not self.config_path or not self.config_path.is_file():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.registry.errors.append(
                {"source": "mcp", "error": f"Invalid {self.config_path}: {exc}"}
            )
            return {}
        servers = data.get("mcpServers", data.get("servers", {}))
        return servers if isinstance(servers, dict) else {}

    async def start(self) -> None:
        specs = {
            k: v for k, v in self._load().items() if isinstance(v, dict) and not v.get("disabled")
        }
        await asyncio.gather(*(self._start_one(name, spec) for name, spec in specs.items()))

    async def _start_one(self, name: str, spec: dict[str, Any]) -> None:
        server = McpServer(name, spec)
        self.servers[name] = server
        if not server.command:
            server.state, server.error = "error", "No command configured."
            return
        try:
            await asyncio.wait_for(server.start(), timeout=START_TIMEOUT + 5)
        except Exception as exc:
            server.state = "error"
            server.error = str(exc) or exc.__class__.__name__
            log.warning("MCP server %s failed to start: %s", name, server.error)
            await server.stop()
            server.state = "error"
            return
        for spec_tool in server.tools:
            self.registry.add(self._wrap(server, spec_tool))
        log.info("MCP server %s: %d tool(s)", name, len(server.tools))

    @staticmethod
    def _wrap(server: McpServer, spec: dict[str, Any]) -> Tool:
        remote = spec["name"]

        async def call(**kwargs: Any) -> str:
            return await server.call_tool(remote, kwargs)

        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("properties", {})
        return Tool(
            name=_tool_name(server.name, remote),
            description=(spec.get("description") or remote)[:1024],
            parameters=schema,
            func=call,
            risk="safe" if server.trust else "confirm",
            category="mcp",
            summary=f"{server.name}: {spec.get('title') or remote}",
            timeout=120.0,
            source=f"mcp:{server.name}",
        )

    async def stop(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.servers.values()), return_exceptions=True)

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "state": s.state,
                "error": s.error,
                "tools": len(s.tools),
                "trusted": s.trust,
            }
            for s in self.servers.values()
        ]
