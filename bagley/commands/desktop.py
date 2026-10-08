"""``bagley overlay-ask``, ``bagley see`` and ``bagley explain``: Bagley on the Hyprland desktop.

* ``overlay-ask TEXT --json`` is what the Quickshell overlay runs: one agent event a line, with
  ``approval.request`` carrying a filled-in ``summary`` so the overlay can say what it would allow.
* ``see [QUESTION]`` sends a screenshot of the focused window with its title, app class, the
  highlighted text and the clipboard to a vision model ("what's this error?").
* ``explain`` explains the highlighted text, or the clipboard when nothing is highlighted.

``see`` and ``explain`` also show the reply as a desktop notification (mako). This process sends
it, so it lands on the screen that was captured even when the server runs on another machine.
Every command talks to the running server and runs the turn here when none answers.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO

from bagley.approvals import summarize
from bagley.client import Client, ServerUnavailable, ask_local
from bagley.desktop.capture import (
    ScreenContext,
    Session,
    Unavailable,
    clipboard,
    focused_window,
    looks_secret,
    screen_context,
    selection,
)

DEFAULT_SEE = "What's on my screen? If there's an error, explain it and how to fix it."
EXPLAIN = (
    "Explain the {what} in plain words and keep it short. If it's code, a command or an error "
    "message, say what it does or what went wrong and how to fix it."
)
MAX_QUESTION = 100_000
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # Terminal escapes, kept off the terminal.


class Ink:
    """ctOS colours for terminal output (24-bit), off for pipes and with NO_COLOR."""

    GRAY = "122;122;122"
    BODY = "202;202;202"
    WHITE = "255;255;255"
    OK = "0;250;154"
    ERROR = "252;62;56"

    def __init__(self, stream: TextIO) -> None:
        isatty = getattr(stream, "isatty", None)
        self.on = bool(isatty and isatty()) and not os.environ.get("NO_COLOR")

    def __call__(self, rgb: str, text: str) -> str:
        return f"\033[38;2;{rgb}m{text}\033[0m" if self.on else text


def clean(text: str) -> str:
    return CONTROL.sub(" ", text)


def write_json(out: TextIO, event: dict[str, Any]) -> None:
    out.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    out.flush()


def plain(markdown: str, limit: int = 500) -> str:
    """Reply text for a notification: one paragraph without code blocks or Markdown marks."""
    text = re.sub(r"```.*?```", "[code]", markdown, flags=re.S)
    text = re.sub(r"[*`#>|]", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def send_note(title: str, body: str, level: str = "info") -> None:
    """Show a desktop notification from this process; nothing happens off the Linux desktop."""
    from bagley import notify

    if notify.desktop_available():
        asyncio.run(notify.send_desktop(notify.Note(title, body, level=level)))


def desktop_session() -> Session:
    return Session.detect()


# One turn ---------------------------------------------------------------------------------------


@dataclass
class Outcome:
    reply: str = ""
    machine: str = ""
    errors: list[str] = field(default_factory=list)


def turn(body: dict[str, Any]) -> tuple[Iterator[dict[str, Any]], bool]:
    """The events of one turn from the running server, or from this process when none answers.
    The flag is True for a local run."""
    client = Client()
    if client.available():
        return client.ask(**body), False
    client.close()
    return ask_local(body), True


def relay(events: Iterator[dict[str, Any]], *, as_json: bool, out: TextIO, err: TextIO) -> Outcome:
    """Print a turn as it streams: JSON lines for the overlay, or the reply on ``out`` with
    progress on ``err``. Returns what the notification needs."""
    ink = Ink(err)
    outcome = Outcome()
    parts: list[str] = []
    templates: dict[str, str] = {}  # Tool call id -> its summary template from tool.start.
    wrote = ended_line = False
    gap = False  # A new step started; separate its text from the last one.

    def say(rgb: str, text: str) -> None:
        nonlocal ended_line
        if wrote and not ended_line:
            out.write("\n")
            out.flush()
            ended_line = True
        print(ink(rgb, clean(text)), file=err, flush=True)

    try:
        for event in events:
            kind = event.get("type")
            call = event.get("call") or {}
            if kind == "tool.start":
                templates[call.get("id", "")] = event.get("summary") or ""
            elif kind == "approval.request":
                summary = summarize(
                    templates.get(call.get("id", ""), ""),
                    call.get("name") or "tool",
                    call.get("arguments") or {},
                )
                event = {**event, "summary": summary}
            elif kind == "model":
                outcome.machine = event.get("machine") or outcome.machine
            elif kind == "text.delta":
                parts.append(event.get("text") or "")
            elif kind == "text.retract":
                joined = "".join(parts)
                parts = [joined[: len(joined) - int(event.get("chars") or 0)]]
            elif kind == "message" and parts and not parts[-1].endswith("\n"):
                parts.append("\n\n")
            elif kind == "error":
                outcome.errors.append(event.get("message") or "Something went wrong.")

            if as_json:
                write_json(out, event)
                continue
            if kind == "text.delta":
                text = clean(event.get("text") or "")
                if gap and wrote:
                    out.write("\n" if ended_line else "\n\n")
                gap = False
                out.write(text)
                out.flush()
                wrote = wrote or bool(text)
                ended_line = text.endswith("\n") if text else ended_line
            elif kind == "message":
                gap = True
            elif kind == "text.retract":
                say(Ink.GRAY, "» ABOVE: REASONING")
            elif kind == "tool.start":
                say(Ink.GRAY, f"» EXEC {call.get('name', '?')}")
            elif kind == "approval.request":
                say(
                    Ink.WHITE,
                    f"AWAIT // {event['summary']} // bagley approve {call.get('id', '')}",
                )
            elif kind == "approval.result":
                say(Ink.GRAY, "[OK] ALLOWED" if event.get("allowed") else "[WARN] DENIED")
            elif kind == "notice":
                say(Ink.GRAY, f"» {event.get('message', '')}")
            elif kind == "error":
                say(Ink.ERROR, f"[CRIT] {event.get('message', '')}")
                if event.get("hint"):
                    say(Ink.GRAY, str(event["hint"]))
    except ServerUnavailable as exc:
        # "413: Images up to 12 MB..." is the server refusing; anything else is the connection.
        message = str(exc) if re.match(r"\d{3}: ", str(exc)) else f"NO SIGNAL: {exc}"
        outcome.errors.append(message)
        if as_json:
            write_json(out, {"type": "error", "message": message})
        else:
            say(Ink.ERROR, f"[CRIT] {message}")
    if wrote and not ended_line and not as_json:
        out.write("\n")
        out.flush()
    outcome.reply = "".join(parts).strip()
    return outcome


def body_for(
    text: str,
    *,
    conversation: str | None = None,
    image: bytes | None = None,
    context: dict[str, str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "text": text[:MAX_QUESTION],
        "source": "overlay",
        "approvals": "ask",
        "context": {k: str(v) for k, v in (context or {}).items() if v},
    }
    if conversation:
        body["conversation_id"] = conversation
    if image:
        body["images"] = [{"data": base64.b64encode(image).decode()}]
    return body


def ask_and_report(args: argparse.Namespace, body: dict[str, Any], *, notify: bool) -> int:
    events, local = turn(body)
    if local:
        message = "NO SERVER // RUNNING HERE"
        if args.json:
            write_json(sys.stdout, {"type": "notice", "message": message})
        else:
            print(Ink(sys.stderr)(Ink.GRAY, f"» {message}"), file=sys.stderr, flush=True)
    outcome = relay(events, as_json=args.json, out=sys.stdout, err=sys.stderr)
    if notify:
        if outcome.errors:
            send_note("BAGLEY // ERROR", plain(outcome.errors[0]), "critical")
        elif outcome.reply:
            title = f"BAGLEY // {outcome.machine.upper()}" if outcome.machine else "BAGLEY"
            send_note(title, plain(outcome.reply))
    return 1 if outcome.errors else 0


def fail(
    args: argparse.Namespace, message: str, notes: Sequence[str] = (), *, notify: bool = False
) -> int:
    """Report a problem found before asking (nothing selected, nothing to see)."""
    if args.json:
        write_json(sys.stdout, {"type": "error", "message": message, "notes": list(notes)})
    else:
        ink = Ink(sys.stderr)
        for note in notes:
            print(ink(Ink.GRAY, f"[WARN] {note}"), file=sys.stderr)
        print(ink(Ink.ERROR, f"[CRIT] {message}"), file=sys.stderr)
    if notify:
        send_note("BAGLEY", message)
    return 1


# Screen context ---------------------------------------------------------------------------------


def context_event(shot: ScreenContext) -> dict[str, Any]:
    """What was captured, as the first JSON line (the overlay reappears when it arrives)."""
    ctx = shot.context
    return {
        "type": "context",
        "image": shot.image is not None,
        "image_bytes": len(shot.image or b""),
        "window_title": ctx.get("window_title", ""),
        "app": ctx.get("app", ""),
        "selection_chars": len(ctx.get("selection", "")),
        "clipboard_chars": len(ctx.get("clipboard", "")),
        "notes": shot.notes,
    }


def report_context(args: argparse.Namespace, shot: ScreenContext, label: str) -> None:
    if args.json:
        write_json(sys.stdout, context_event(shot))
        return
    ink = Ink(sys.stderr)
    ctx = shot.context
    where = " // ".join(clean(ctx[k])[:60] for k in ("app", "window_title") if ctx.get(k))
    print(ink(Ink.GRAY, f"» {label}" + (f" // {where}" if where else "")), file=sys.stderr)
    found = []
    if shot.image:
        found.append(f"IMAGE {max(1, len(shot.image) // 1024)}K")
    for key, name in (("selection", "SELECTION"), ("clipboard", "CLIPBOARD")):
        if ctx.get(key):
            found.append(f"{name} {len(ctx[key])} CHARS")
    if found:
        print(ink(Ink.OK, "[OK] ") + ink(Ink.GRAY, " // ".join(found)), file=sys.stderr)
    for note in shot.notes:
        print(ink(Ink.GRAY, f"[WARN] {note}"), file=sys.stderr)
    sys.stderr.flush()


def look(args: argparse.Namespace, *, image: bool) -> ScreenContext:
    shot = screen_context(desktop_session(), image=image)
    report_context(args, shot, "SEE")
    return shot


# Commands ---------------------------------------------------------------------------------------


def _utf8() -> None:
    # Pipes default to the locale's encoding; the overlay reads UTF-8.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def cmd_overlay_ask(args: argparse.Namespace) -> int:
    _utf8()
    text = " ".join(args.text).strip()
    if not text and not args.see:
        print('Usage: bagley overlay-ask "question"  (or --see)', file=sys.stderr)
        return 2
    image, context = None, {}
    if args.see:
        shot = look(args, image=True)
        image, context = shot.image, shot.context
        text = text or DEFAULT_SEE
    body = body_for(text, conversation=args.conversation, image=image, context=context)
    try:
        return ask_and_report(args, body, notify=args.notify)
    except KeyboardInterrupt:
        return 130


def cmd_see(args: argparse.Namespace) -> int:
    _utf8()
    question = " ".join(args.question).strip() or DEFAULT_SEE
    shot = look(args, image=not args.no_image)
    if shot.image is None and not shot.context:
        reason = "NOTHING TO SEE: no window, screenshot, selection or clipboard."
        return fail(args, reason, notify=not args.no_notify)  # The notes are already out.
    body = body_for(
        question, conversation=args.conversation, image=shot.image, context=shot.context
    )
    try:
        return ask_and_report(args, body, notify=not args.no_notify)
    except KeyboardInterrupt:
        return 130


def cmd_explain(args: argparse.Namespace) -> int:
    _utf8()
    session = desktop_session()
    notes: list[str] = []
    context: dict[str, str] = {}
    with contextlib.suppress(Unavailable):
        window = focused_window(session)
        if window:
            context.update(window_title=window.title, app=window.app)
    key, what, text = "selection", "selected text", ""
    try:
        text = selection(session)
    except Unavailable as exc:
        notes.append(str(exc))
    if not text.strip():
        key, what, text = "clipboard", "text in my clipboard", ""
        try:
            text = clipboard(session)
        except Unavailable as exc:
            notes.append(str(exc))
        if text.strip() and looks_secret(text):
            notes.append("CLIPBOARD SKIPPED: LOOKS LIKE A SECRET")
            text = ""
    notes = list(dict.fromkeys(notes))
    if not text.strip():
        return fail(
            args, "NOTHING SELECTED. Highlight some text first.", notes, notify=not args.no_notify
        )
    context[key] = text
    report_context(args, ScreenContext(None, context, notes), "EXPLAIN")
    body = body_for(EXPLAIN.format(what=what), context=context)
    try:
        return ask_and_report(args, body, notify=not args.no_notify)
    except KeyboardInterrupt:
        return 130


def register(sub: argparse._SubParsersAction) -> None:
    overlay = sub.add_parser(
        "overlay-ask", help="Ask from the desktop overlay; streams the turn (--json for Quickshell)"
    )
    overlay.add_argument("text", nargs="*", help="The question")
    overlay.add_argument("--json", action="store_true", help="One JSON event per line")
    overlay.add_argument("-c", "--conversation", help="Continue a conversation by id")
    overlay.add_argument("--notify", action="store_true", help="Also show the reply in mako")
    overlay.add_argument(
        "--see", action="store_true", help="Attach the focused window, selection and clipboard"
    )
    overlay.set_defaults(func=cmd_overlay_ask)

    see = sub.add_parser(
        "see", help="Ask about what's on screen (screenshot, selection, clipboard)"
    )
    see.add_argument("question", nargs="*", help=f'Default: "{DEFAULT_SEE}"')
    see.add_argument("--json", action="store_true", help="One JSON event per line")
    see.add_argument("--no-image", action="store_true", help="Send the window title and text only")
    see.add_argument("--no-notify", action="store_true", help="Don't show the reply in mako")
    see.add_argument("-c", "--conversation", help="Continue a conversation by id")
    see.set_defaults(func=cmd_see)

    explain = sub.add_parser("explain", help="Explain the highlighted text (or the clipboard)")
    explain.add_argument("--json", action="store_true", help="One JSON event per line")
    explain.add_argument("--no-notify", action="store_true", help="Don't show the reply in mako")
    explain.set_defaults(func=cmd_explain)
