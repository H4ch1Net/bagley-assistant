"""``bagley suggest`` and ``bagley why``: the shell co-pilot on the command line.

``bagley suggest find files over 1GB`` prints one command line and nothing else, so it works in
``$(...)``. A dangerous one comes out commented, under a ``# CAREFUL: <reason>`` line. ``--zsh``
prints what the widget in ``desktop/zsh/bagley.zsh`` reads instead:

    <command>
    #danger: <why it is risky>     (only when flagged)
    #note: <what it does>          (when the model said)
    #error: <message>              (exit status 1, and no command line)

``bagley why`` explains a failed command. The zsh plugin passes the command, its status and the
captured output; by hand, pipe the output in: ``make 2>&1 | bagley why``.

Both ask the running server when there is one and work in this process otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable
from typing import IO, TYPE_CHECKING, Any

from bagley.cli import Style
from bagley.client import Client, ServerUnavailable

if TYPE_CHECKING:
    from bagley.runtime import Runtime

REQUEST_TIMEOUT = 180.0  # A cold model can take a while to load.
READ_HEAD = 16_000  # Characters of piped output kept from the start...
READ_TAIL = 48_000  # ...and from the end.
READ_LIMIT = 64_000_000  # Stop reading after this much.

Work = Callable[["Runtime"], Awaitable[Any]]


class Failure(Exception):
    """Neither the server nor this process could answer."""


class Tint:
    """ctOS terminal colours (24-bit) for one stream, plain for NO_COLOR and non-terminals."""

    GRAY, BODY, WHITE, ERROR = "122;122;122", "202;202;202", "255;255;255", "252;62;56"

    def __init__(self, stream: IO[str] | None = None, *, on: bool | None = None) -> None:
        self.on = Style(stream or sys.stdout).on if on is None else on

    def paint(self, rgb: str, text: str) -> str:
        return f"\033[38;2;{rgb}m{text}\033[0m" if self.on and text else text

    def gray(self, text: str) -> str:
        return self.paint(self.GRAY, text)

    def body(self, text: str) -> str:
        return self.paint(self.BODY, text)

    def white(self, text: str) -> str:
        return self.paint(self.WHITE, text)

    def error(self, text: str) -> str:
        return self.paint(self.ERROR, text)


# Server or this process ------------------------------------------------------------------


def local_runtime() -> Runtime:
    """A runtime in this process, for when no server is running."""
    from bagley.config import ServerConfig
    from bagley.runtime import Runtime

    return Runtime(ServerConfig.from_env())


async def _in_process(work: Work) -> dict[str, Any]:
    rt = local_runtime()
    try:
        return (await work(rt)).to_dict()
    finally:
        await rt.aclose()


def _detail(message: str) -> str:
    code, sep, rest = message.partition(": ")
    return rest if sep and code.isdigit() else message


def call(path: str, body: dict[str, Any], work: Work) -> dict[str, Any]:
    """Ask the running server, or this process when none answers (or it predates ``path``)."""
    from bagley.llm import LLMError

    with Client(timeout=REQUEST_TIMEOUT) as client:
        if client.available():
            try:
                return client.post(path, body)
            except ServerUnavailable as exc:
                if not str(exc).startswith("404"):
                    raise Failure(_detail(str(exc))) from exc
    try:
        return asyncio.run(_in_process(work))
    except LLMError as exc:
        raise Failure(f"{exc.message} {exc.hint}".strip()) from exc
    except ValueError as exc:
        raise Failure(str(exc)) from exc


def _utf8(*streams: Any) -> None:
    # Redirected streams default to the locale's code page on Windows; commands are UTF-8.
    for stream in streams:
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _shell(args: argparse.Namespace) -> str:
    from bagley.shellhelp import shell_name

    default = "powershell" if os.name == "nt" else "sh"
    return shell_name(args.shell or os.environ.get("SHELL", ""), default)


# bagley suggest --------------------------------------------------------------------------


def cmd_suggest(args: argparse.Namespace) -> int:
    from bagley import shellhelp

    _utf8(sys.stdout, sys.stderr)
    words = list(args.request)
    if words[:1] == ["--"]:
        words = words[1:]
    request = " ".join(words).strip()
    if not request:
        if args.zsh:
            print("#error: say what you want after ??, e.g. ?? find files over 1GB")
        else:
            print('Usage: bagley suggest "what the command should do"', file=sys.stderr)
        return 2
    cwd = args.cwd or os.getcwd()
    shell = "zsh" if args.zsh else _shell(args)
    body = {"request": request, "cwd": cwd, "shell": shell}
    try:
        result = call(
            "/api/shell/suggest", body, lambda rt: shellhelp.suggest(rt, request, cwd, shell)
        )
    except Failure as exc:
        if args.zsh:
            print(f"#error: {_one_line(str(exc))}")
        else:
            print(Tint(sys.stderr).error(f"[ERR] {exc}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    command = _one_line(result["command"])
    danger = _one_line(result.get("danger") or "")
    note = _one_line(result.get("explanation") or "")
    if args.zsh:
        print(command)
        if danger:
            print(f"#danger: {danger}")
        if note:
            print(f"#note: {note}")
        return 0
    if danger:  # Commented out, so pasting or eval-ing it can't run it by accident.
        print(f"# CAREFUL: {danger}")
        print(f"# {command}")
    else:
        print(command)
    if note and sys.stderr.isatty():
        print(Tint(sys.stderr).gray(f"» {note}"), file=sys.stderr)
    return 0


# bagley why ------------------------------------------------------------------------------


def read_bounded(stream: IO[str]) -> str:
    """Read piped output without keeping all of it: the head, a marker and the tail."""
    head = stream.read(READ_HEAD)
    tail, total = "", len(head)
    while total < READ_LIMIT:
        chunk = stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        tail = (tail + chunk)[-READ_TAIL:]
    cut = total - len(head) - len(tail)
    return f"{head}\n…[{cut} characters cut]…\n{tail}" if cut > 0 else head + tail


def render_why(result: dict[str, Any], status: int | None, tint: Tint) -> str:
    """The explanation as ctOS terminal text: a gray readout, the body, then the fix."""
    header = ["» WHY"]
    if status is not None:
        header.append(f"EXIT {status}")
    if result.get("machine"):
        header.append(str(result["machine"]).upper())
    lines = [tint.gray(" // ".join(header))]
    lines += [tint.body(line) for line in str(result.get("text", "")).splitlines() if line.strip()]
    fix = _one_line(result.get("fix") or "")
    if fix and result.get("danger"):
        lines.append(tint.gray("FIX  ") + tint.white(f"# {fix}"))
        lines.append(tint.error("CAREFUL  ") + tint.body(_one_line(result["danger"])))
    elif fix:
        lines.append(tint.gray("FIX  ") + tint.white(fix))
    return "\n".join(lines)


def cmd_why(args: argparse.Namespace) -> int:
    from bagley import shellhelp

    streams = [sys.stdout, sys.stderr]
    piped = sys.stdin is not None and not sys.stdin.isatty()
    _utf8(*streams, *([sys.stdin] if piped else []))
    out, err = Tint(sys.stdout), Tint(sys.stderr)
    output = ""
    if args.output_file:
        try:
            with open(args.output_file, encoding="utf-8", errors="replace") as f:
                output = read_bounded(f)
        except OSError as exc:
            print(err.error(f"[ERR] Can't read {args.output_file}: {exc}"), file=sys.stderr)
            return 1
    elif piped:
        output = read_bounded(sys.stdin)
    command = (args.failed or "").strip()
    if not command and not output.strip():
        print(err.gray("» WHY // NO FAILED COMMAND ON RECORD"), file=sys.stderr)
        print(
            err.body("Load the zsh plugin, or pipe the output in: ")
            + err.white("cmd 2>&1 | bagley why"),
            file=sys.stderr,
        )
        return 1
    cwd = args.cwd or os.getcwd()
    shell = _shell(args)
    body = {
        "command": command,
        "status": args.status,
        "output": output,
        "cwd": cwd,
        "shell": shell,
    }
    try:
        result = call(
            "/api/shell/why",
            body,
            lambda rt: shellhelp.why(rt, command, args.status, output, cwd, shell),
        )
    except Failure as exc:
        print(err.gray("» WHY // ") + err.error(f"[ERR] {exc}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(render_why(result, args.status, out))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    suggest = sub.add_parser(
        "suggest", help="Propose one shell command for a request (prints it, never runs it)"
    )
    suggest.add_argument("request", nargs="*", help="What the command should do")
    suggest.add_argument(
        "--zsh", action="store_true", help="Output for the zsh widget: the command, then #lines"
    )
    suggest.add_argument("--cwd", help="Directory the command is for (default: this one)")
    suggest.add_argument("--shell", help="Shell to write for (default: $SHELL)")
    suggest.set_defaults(func=cmd_suggest)

    why = sub.add_parser("why", help="Explain why a command failed, with a fix to try")
    # Not dest="command": that is where the main parser keeps the subcommand's name.
    why.add_argument("--command", dest="failed", help="The command line that failed")
    why.add_argument("--status", type=int, help="Its exit status")
    why.add_argument("--cwd", help="Where it ran (default: this directory)")
    why.add_argument("--output-file", help="File holding its output (default: stdin when piped)")
    why.add_argument("--shell", help="Shell the fix is for (default: $SHELL)")
    why.set_defaults(func=cmd_why)
