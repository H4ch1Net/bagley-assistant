"""Command line entry point: ``bagley [serve|chat|ask|doctor]``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from bagley import __version__
from bagley.config import ServerConfig, load_dotenv


class Style:
    def __init__(self, stream: Any = sys.stdout) -> None:
        self.on = stream.isatty() and not os.environ.get("NO_COLOR")

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def accent(self, t: str) -> str:
        return self._wrap("36", t)

    def ok(self, t: str) -> str:
        return self._wrap("32", t)

    def bad(self, t: str) -> str:
        return self._wrap("31", t)

    def warn(self, t: str) -> str:
        return self._wrap("33", t)


S = Style()


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _config(args: argparse.Namespace) -> ServerConfig:
    config = ServerConfig.from_env()
    if getattr(args, "host", None):
        config.host = args.host
    if getattr(args, "port", None):
        config.port = args.port
    return config


async def _probe(config: ServerConfig) -> dict[str, Any]:
    from bagley.runtime import Runtime

    rt = Runtime(config)
    try:
        return await rt.health()
    finally:
        await rt.aclose()


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from bagley.server import create_app

    config = _config(args)
    if not config.is_loopback and not config.token:
        config.token = secrets.token_urlsafe(18)
        print(
            S.warn(
                "  Listening beyond localhost: generated an access token (set BAGLEY_TOKEN to fix one)."
            )
        )
    if not _port_free(config.host, config.port):
        print(S.bad(f"  Port {config.port} on {config.host} is already in use."))
        print(
            f"  Is Bagley already running? Otherwise pick another port: {S.bold('bagley --port 8766')}"
        )
        return 1

    health = asyncio.run(_probe(config))
    shown_host = "127.0.0.1" if config.host in ("0.0.0.0", "::") else config.host
    url = f"http://{shown_host}:{config.port}/"
    if config.token:
        url += f"?token={config.token}"

    print()
    print(f"  {S.accent('◉')} {S.bold('Bagley')} {S.dim('v' + __version__)}")
    print(f"  {S.dim('Open')}       {S.accent(url)}")
    if health["ok"]:
        model = health.get("model") or "none installed"
        print(
            f"  {S.dim('Model')}      {model} {S.dim('via ' + health['provider'] + ' at ' + health['base_url'])}"
        )
    else:
        print(f"  {S.dim('Model')}      {S.warn(health.get('error', 'unavailable'))}")
        if health.get("hint"):
            print(f"             {S.dim(health['hint'])}")
    print(f"  {S.dim('Workspace')}  {config.workspace}")
    print(f"  {S.dim('Data')}       {config.data_dir}")
    print(S.dim("  Press Ctrl+C to stop."))
    print()

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        create_app(config=config),
        host=config.host,
        port=config.port,
        log_level=args.log_level,
        ws_ping_interval=20,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _config(args)

    async def run() -> int:
        from bagley.runtime import Runtime

        rt = Runtime(config)
        failures = 0

        def line(ok: bool | None, label: str, detail: str = "") -> None:
            nonlocal failures
            mark = S.ok("✓") if ok else (S.warn("!") if ok is None else S.bad("✗"))
            failures += ok is False
            print(f"  {mark} {label:<22} {S.dim(detail)}")

        try:
            await rt.start()
            prefs, locked = rt.preferences()
            print(f"\n  {S.bold('Bagley doctor')} {S.dim('v' + __version__)}\n")
            line(True, "Python", sys.version.split()[0])
            line(True, "Data directory", str(config.data_dir))
            writable = os.access(config.workspace or ".", os.W_OK)
            line(writable, "Workspace", str(config.workspace))
            health = await rt.health()
            if health["ok"]:
                line(
                    True,
                    "Model server",
                    f"{health['provider']} {health.get('version', '')} at {prefs.base_url}",
                )
                line(health["models"] > 0, "Installed models", str(health["models"]))
                if health.get("model"):
                    caps = health.get("capabilities") or {}
                    tools = caps.get("tools")
                    detail = (
                        "native tools"
                        if tools
                        else ("text-based tools" if tools is False else "tools: unknown")
                    )
                    if caps.get("thinking"):
                        detail += ", reasoning"
                    installed = health.get("installed", True)
                    line(
                        installed,
                        "Model",
                        f"{health['model']} ({detail})"
                        if installed
                        else f"{health['model']} is not installed",
                    )
            else:
                line(False, "Model server", health.get("error", ""))
                if health.get("hint"):
                    print(f"      {S.dim(health['hint'])}")
            enabled = rt.registry.enabled(prefs.disabled_tools)
            line(True, "Tools", f"{len(enabled)} enabled: {', '.join(t.name for t in enabled)}")
            line(
                None if not config.enable_shell else True,
                "Shell tool",
                "enabled" if config.enable_shell else "disabled (BAGLEY_ENABLE_SHELL)",
            )
            for err in rt.registry.errors:
                line(False, err["source"], err["error"])
            for server in rt.mcp.status():
                line(
                    server["state"] == "running",
                    f"MCP {server['name']}",
                    server["error"] or f"{server['tools']} tools",
                )
            if locked:
                line(None, "Env overrides", ", ".join(sorted(locked)))
            print()
        finally:
            await rt.aclose()
        return 1 if failures else 0

    return asyncio.run(run())


def cmd_chat(args: argparse.Namespace) -> int:
    from bagley.agent import Agent, RunRequest
    from bagley.llm import ToolCall
    from bagley.runtime import Runtime
    from bagley.tools import Tool

    config = _config(args)

    async def run() -> int:
        rt = Runtime(config)
        await rt.start()
        agent = Agent(rt)
        cid: str | None = args.conversation
        always: set[str] = set()
        state = {"reasoning": False}

        async def emit(event: dict[str, Any]) -> None:
            nonlocal cid
            kind = event["type"]
            if kind == "conversation":
                cid = event["conversation"]["id"]
            elif kind == "reasoning.delta":
                if not state["reasoning"]:
                    print(S.dim("  thinking… "), end="")
                    state["reasoning"] = True
                print(S.dim(event["text"]), end="", flush=True)
            elif kind == "text.delta":
                if state["reasoning"]:
                    print()
                    state["reasoning"] = False
                print(event["text"], end="", flush=True)
            elif kind == "text.retract":
                print(S.dim("\n  ↑ that was Bagley thinking"))
            elif kind == "tool.start":
                call = event["call"]
                print(
                    S.dim(
                        f"\n  ⚙ {call['name']}({json.dumps(call['arguments'], ensure_ascii=False)[:120]})"
                    )
                )
            elif kind == "tool.end":
                mark = S.ok("✓") if event["ok"] else S.bad("✗")
                print(S.dim(f"  {mark} {event['duration_ms']} ms"))
            elif kind == "notice":
                print(S.warn(f"\n  {event['message']}"))
            elif kind == "error":
                print(S.bad(f"\n  {event['message']}"))
                if event.get("hint"):
                    print(S.dim(f"  {event['hint']}"))

        async def approve(call: ToolCall, tool: Tool) -> bool:
            if tool.name in always:
                return True
            args_text = json.dumps(call.arguments, ensure_ascii=False, indent=2)
            print(S.warn(f"\n  Bagley wants to run {tool.name}:\n") + S.dim(args_text))
            answer = (
                (await asyncio.to_thread(input, "  Allow? [y]es / [n]o / [a]lways: "))
                .strip()
                .lower()
            )
            if answer.startswith("a"):
                always.add(tool.name)
            return answer.startswith(("y", "a"))

        print(
            f"\n  {S.accent('◉')} {S.bold('Bagley')} {S.dim('· /new starts over · /exit quits · Ctrl+C stops a reply')}"
        )
        try:
            while True:
                try:
                    text = (await asyncio.to_thread(input, f"\n{S.accent('you ›')} ")).strip()
                except EOFError:
                    break
                if not text:
                    continue
                if text in ("/exit", "/quit"):
                    break
                if text == "/new":
                    cid = None
                    print(S.dim("  New conversation."))
                    continue
                print(f"\n{S.accent('bagley ›')} ", end="", flush=True)
                task = asyncio.create_task(
                    agent.run(RunRequest(text=text, conversation_id=cid), emit, approve)
                )
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current and hasattr(current, "uncancel"):
                        current.uncancel()
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    print(S.dim("\n  Stopped."))
                print()
        finally:
            await rt.aclose()
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print()
        return 0


MAX_STDIN = 60_000


def cmd_ask(args: argparse.Namespace) -> int:
    """One question, answer on stdout. Piped input is attached; progress goes to stderr."""
    from bagley.agent import Agent, RunRequest
    from bagley.llm import ToolCall
    from bagley.runtime import Runtime
    from bagley.tools import Tool

    # Redirected streams default to the locale's code page on Windows; answers are UTF-8.
    streams = [sys.stdout, sys.stderr] + ([] if sys.stdin.isatty() else [sys.stdin])
    for stream in streams:
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    question = " ".join(args.question).strip()
    piped = "" if sys.stdin.isatty() else sys.stdin.read()
    if len(piped) > MAX_STDIN:
        piped = piped[:MAX_STDIN] + "\n…[input truncated]"
    if piped.strip():
        question = f"{question or 'Look at this.'}\n\n<input>\n{piped.strip()}\n</input>"
    if not question:
        print('Usage: bagley ask "question"  (or pipe text in)', file=sys.stderr)
        return 2
    err = Style(sys.stderr)

    def note(text: str) -> None:
        if not args.quiet:
            print(err.dim(text), file=sys.stderr, flush=True)

    async def run() -> int:
        rt = Runtime(_config(args))
        await rt.start()
        failed = False
        ended_line = True

        async def emit(event: dict[str, Any]) -> None:
            nonlocal failed, ended_line
            kind = event["type"]
            if kind == "text.delta":
                sys.stdout.write(event["text"])
                sys.stdout.flush()
                ended_line = event["text"].endswith("\n")
            elif kind == "text.retract":
                if not ended_line:
                    sys.stdout.write("\n")
                    ended_line = True
                note("↑ that was Bagley thinking")
            elif kind == "tool.start":
                call = event["call"]
                note(f"⚙ {call['name']} {json.dumps(call['arguments'], ensure_ascii=False)[:100]}")
            elif kind == "notice":
                note(event["message"])
            elif kind == "error":
                failed = True
                print(err.bad(event["message"]), file=sys.stderr)
                if event.get("hint"):
                    print(err.dim(event["hint"]), file=sys.stderr)

        async def approve(call: ToolCall, tool: Tool) -> bool:
            if not args.yes:
                note(f"✗ skipped {tool.name}: it needs approval (pass --yes to allow)")
            return bool(args.yes)

        try:
            await Agent(rt).run(
                RunRequest(text=question, conversation_id=args.conversation), emit, approve
            )
        finally:
            await rt.aclose()
        if not ended_line:
            sys.stdout.write("\n")
        return 1 if failed else 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bagley", description="Local-first AI assistant.")
    parser.add_argument("--version", action="version", version=f"bagley {__version__}")
    sub = parser.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser, default: Any = None) -> None:
        p.add_argument(
            "--host", default=default, help="Bind address (default 127.0.0.1, env BAGLEY_HOST)"
        )
        p.add_argument(
            "--port", type=int, default=default, help="Port (default 8765, env BAGLEY_PORT)"
        )

    serve = sub.add_parser("serve", help="Start the web UI (default)")
    common(serve, argparse.SUPPRESS)
    serve.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    serve.add_argument(
        "--log-level", default="warning", choices=["debug", "info", "warning", "error"]
    )

    chat = sub.add_parser("chat", help="Chat in the terminal")
    chat.add_argument("-c", "--conversation", help="Continue a conversation by id")

    ask = sub.add_parser("ask", help="Ask one question; the answer goes to stdout")
    ask.add_argument("question", nargs="*", help="The question. Text piped in is attached.")
    ask.add_argument("-y", "--yes", action="store_true", help="Allow tools that need approval")
    ask.add_argument("-q", "--quiet", action="store_true", help="Don't show tool activity")
    ask.add_argument("-c", "--conversation", help="Continue a conversation by id")

    sub.add_parser("doctor", help="Check configuration and model server")

    from bagley.commands import register_all

    register_all(sub)
    common(parser)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--log-level", default="warning", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    from bagley.service import env_path

    load_dotenv(Path.cwd() / ".env")
    load_dotenv(env_path())  # ~/.config/bagley/env (`bagley env`); the local .env wins.
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(getattr(args, "log_level", "warning")).upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    command = args.command or "serve"
    handlers = {"serve": cmd_serve, "chat": cmd_chat, "ask": cmd_ask, "doctor": cmd_doctor}
    if command in handlers:
        return handlers[command](args)
    return int(args.func(args) or 0)
