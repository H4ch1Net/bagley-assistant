from __future__ import annotations

import base64
import json
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bagley.cli import build_parser
from bagley.client import Client, ServerUnavailable
from bagley.commands import desktop as cmd
from bagley.desktop import capture
from bagley.desktop.capture import Result, Session, Unavailable, Window
from bagley.server import create_app
from tests.mock_llm import Reply

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WINDOW = {
    "address": "0x55d1c0de",
    "mapped": True,
    "hidden": False,
    "at": [12, 49],
    "size": [1896, 1019],
    "workspace": {"id": 1, "name": "1"},
    "floating": False,
    "monitor": 0,
    "class": "kitty",
    "title": "~/code/app - pytest",
    "initialClass": "kitty",
    "pid": 4242,
}
MONITORS = [
    {"id": 0, "name": "eDP-1", "focused": False},
    {"id": 1, "name": "DP-2", "focused": True},
]
TEXT_TYPES = "text/plain;charset=utf-8\ntext/plain\nUTF8_STRING\n"
GEOMETRY = "12,49 1896x1019"
WAYLAND = {"WAYLAND_DISPLAY": "wayland-1", "HYPRLAND_INSTANCE_SIGNATURE": "abc_123"}


class FakeDesktop:
    """Stands in for hyprctl, grim and wl-paste: answers by the joined command line."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, args: list[str], env: Any, timeout: float) -> Result:
        self.calls.append(list(args))
        self.envs.append(dict(env))
        answer = self.answers.get(" ".join(args))
        if answer is None:
            return Result(1, b"", b"not faked")
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Result):
            return answer
        return Result(0, answer if isinstance(answer, bytes) else answer.encode())

    def ran(self, program: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == program]


def fake_session(
    answers: dict[str, Any],
    env: dict[str, str] | None = None,
    installed: tuple[str, ...] = ("hyprctl", "grim", "wl-paste"),
) -> tuple[Session, FakeDesktop]:
    fake = FakeDesktop(answers)

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in installed else None

    return Session(dict(WAYLAND if env is None else env), fake, which), fake


def desktop_answers(**overrides: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "hyprctl -j activewindow": json.dumps(WINDOW),
        "hyprctl -j monitors": json.dumps(MONITORS),
        f"grim -g {GEOMETRY} -": PNG,
        "wl-paste --primary --list-types": TEXT_TYPES,
        "wl-paste --primary --no-newline --type text/plain;charset=utf-8": "TypeError: x is None",
        "wl-paste --list-types": TEXT_TYPES,
        "wl-paste --no-newline --type text/plain;charset=utf-8": "pip install -e .",
    }
    answers.update(overrides)
    return answers


# Capture ----------------------------------------------------------------------------------------


def test_parse_window():
    window = capture.parse_window(json.dumps(WINDOW).encode())
    assert window == Window(
        title="~/code/app - pytest",
        app="kitty",
        x=12,
        y=49,
        width=1896,
        height=1019,
        address="0x55d1c0de",
        monitor=0,
    )
    assert window.geometry == GEOMETRY
    assert capture.parse_window(b"{}") is None  # Nothing focused.
    assert capture.parse_window(json.dumps({**WINDOW, "size": "big"})) is None
    assert capture.parse_window(json.dumps({**WINDOW, "at": [1.6, 2.2]})).geometry == (
        "2,2 1896x1019"
    )
    with pytest.raises(Unavailable, match="HYPRCTL: Couldn't connect"):
        capture.parse_window(b"Couldn't connect to /run/user/1000/hypr/x/.socket.sock")


def test_session_finds_hyprland_and_wayland_without_env(tmp_path):
    for name, age in (("old_sig", 1000), ("new_sig", 10)):
        sock = tmp_path / "hypr" / name / ".socket.sock"
        sock.parent.mkdir(parents=True)
        sock.write_bytes(b"")
        mtime = os.stat(sock).st_mtime - age
        os.utime(sock, (mtime, mtime))
    (tmp_path / "hypr" / "stale").mkdir()  # No socket: not a running instance.
    (tmp_path / "wayland-1").write_bytes(b"")
    (tmp_path / "wayland-1.lock").write_bytes(b"")
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "PATH": "/usr/bin"}
    session = Session.detect(env, legacy_hypr=tmp_path / "none")
    assert session.env["WAYLAND_DISPLAY"] == "wayland-1"
    assert session.env["HYPRLAND_INSTANCE_SIGNATURE"] == "new_sig"
    assert session.env["PATH"] == "/usr/bin"

    kept = Session.detect({**env, **WAYLAND}, legacy_hypr=tmp_path / "none")
    assert kept.env["HYPRLAND_INSTANCE_SIGNATURE"] == "abc_123"

    empty = tmp_path / "empty"
    empty.mkdir()
    bare = Session.detect({"XDG_RUNTIME_DIR": str(empty)}, legacy_hypr=tmp_path / "none")
    assert "WAYLAND_DISPLAY" not in bare.env and "HYPRLAND_INSTANCE_SIGNATURE" not in bare.env


def test_focused_window_asks_hyprland():
    session, fake = fake_session(desktop_answers())
    window = capture.focused_window(session)
    assert window is not None and window.app == "kitty"
    assert fake.calls == [["hyprctl", "-j", "activewindow"]]
    assert fake.envs[0]["HYPRLAND_INSTANCE_SIGNATURE"] == "abc_123"

    session, _ = fake_session({"hyprctl -j activewindow": "{}"})
    assert capture.focused_window(session) is None


def test_off_wayland_everything_explains_itself():
    session, fake = fake_session(desktop_answers(), env={})
    with pytest.raises(Unavailable, match="HYPRLAND NOT RUNNING"):
        capture.focused_window(session)
    with pytest.raises(Unavailable, match="NO WAYLAND SESSION"):
        capture.selection(session)
    shot = capture.screen_context(session)
    assert shot.image is None and shot.context == {}
    assert shot.notes == ["HYPRLAND NOT RUNNING", "NO WAYLAND SESSION"]
    assert fake.calls == []  # Nothing ran.


def test_missing_programs_are_named():
    session, fake = fake_session(desktop_answers(), installed=("hyprctl",))
    shot = capture.screen_context(session)
    assert shot.image is None
    assert shot.context == {"window_title": "~/code/app - pytest", "app": "kitty"}
    assert shot.notes == ["GRIM NOT INSTALLED", "WL-PASTE NOT INSTALLED"]
    assert not fake.ran("grim") and not fake.ran("wl-paste")


def test_grab_window_falls_back_to_the_output_then_everything():
    session, fake = fake_session(desktop_answers())
    assert capture.grab_window(GEOMETRY, session) == PNG
    assert fake.calls == [["grim", "-g", GEOMETRY, "-"]]

    # The window capture fails: the focused output (from hyprctl), then every output.
    session, fake = fake_session(
        desktop_answers(**{f"grim -g {GEOMETRY} -": Result(1), "grim -o DP-2 -": PNG})
    )
    assert capture.capture(Window("t", "a", 12, 49, 1896, 1019), session) == (PNG, "output")
    session, fake = fake_session(
        desktop_answers(**{f"grim -g {GEOMETRY} -": b"garbage", "grim -": PNG})
    )
    assert capture.capture(GEOMETRY, session) == (PNG, "screen")
    assert fake.ran("grim")[-1] == ["grim", "-"]

    # A malformed rectangle never reaches grim's -g.
    session, fake = fake_session(desktop_answers(**{"grim -": PNG}))
    assert capture.capture("0,0 10x10; rm -rf ~", session)[1] == "screen"
    assert all("-g" not in call for call in fake.ran("grim"))

    session, _ = fake_session({})
    with pytest.raises(Unavailable, match="GRIM FAILED"):
        capture.grab_window(None, session)


def test_large_screenshots_are_sent_as_jpeg(monkeypatch):
    monkeypatch.setattr(capture, "MAX_IMAGE", 100)
    big = PNG + b"\x01" * 200
    session, fake = fake_session(
        desktop_answers(
            **{f"grim -g {GEOMETRY} -": big, f"grim -t jpeg -q 85 -g {GEOMETRY} -": JPEG}
        )
    )
    assert capture.grab_window(GEOMETRY, session) == JPEG
    assert fake.ran("grim")[-1] == ["grim", "-t", "jpeg", "-q", "85", "-g", GEOMETRY, "-"]


def test_selection_and_clipboard_read_text_only():
    session, fake = fake_session(desktop_answers())
    assert capture.selection(session) == "TypeError: x is None"
    assert capture.clipboard(session) == "pip install -e ."
    assert fake.calls[:2] == [
        ["wl-paste", "--primary", "--list-types"],
        ["wl-paste", "--primary", "--no-newline", "--type", "text/plain;charset=utf-8"],
    ]

    session, fake = fake_session({"wl-paste --list-types": "image/png\n"})
    with pytest.raises(Unavailable, match=r"CLIPBOARD NOT TEXT \(image/png\)"):
        capture.clipboard(session)
    assert len(fake.calls) == 1  # The image itself is never read.

    hinted = "text/plain\nx-kde-passwordManagerHint\n"
    session, fake = fake_session({"wl-paste --list-types": hinted})
    with pytest.raises(Unavailable, match="PASSWORD MANAGER"):
        capture.clipboard(session)

    session, _ = fake_session({"wl-paste --primary --list-types": Result(1, b"", b"No selection")})
    assert capture.selection(session) == ""

    only_html = {
        "wl-paste --list-types": "text/html\n",
        "wl-paste --no-newline --type text/html": "<b>hi</b>",
    }
    session, _ = fake_session(only_html)
    assert capture.clipboard(session) == "<b>hi</b>"

    huge = {
        "wl-paste --list-types": "UTF8_STRING\n",
        "wl-paste --no-newline --type UTF8_STRING": "é" * 20_000 + "\x00",
    }
    session, _ = fake_session(huge)
    assert capture.clipboard(session) == "é" * capture.MAX_TEXT

    binary = {
        "wl-paste --list-types": "text/plain\n",
        "wl-paste --no-newline --type text/plain": bytes(range(128, 256)) * 4,
    }
    session, _ = fake_session(binary)
    with pytest.raises(Unavailable, match="binary"):
        capture.clipboard(session)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("sk-ant-api03-abcdefghijklmnopqrstuvwx", True),
        ("ghp_" + "a" * 36, True),
        ("AKIAIOSFODNN7EXAMPLE", True),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=", True),
        ("hunter2", False),
        ("Tr0ub4dor&3xQ", True),  # A generated password: every character class, no spaces.
        ("TypeError: 'NoneType' object is not subscriptable", False),
        ("https://Example.com/a1?b=C2", False),
        ("pip install -e .", False),
    ],
)
def test_looks_secret(text, secret):
    assert capture.looks_secret(text) is secret


def test_screen_context_gathers_everything():
    session, _ = fake_session(desktop_answers())
    shot = capture.screen_context(session)
    assert shot.image == PNG
    assert shot.context == {
        "window_title": "~/code/app - pytest",
        "app": "kitty",
        "selection": "TypeError: x is None",
        "clipboard": "pip install -e .",
    }
    assert shot.notes == []

    secret = desktop_answers(
        **{"wl-paste --no-newline --type text/plain;charset=utf-8": "ghp_" + "b" * 36}
    )
    shot = capture.screen_context(fake_session(secret)[0], image=False)
    assert shot.image is None and "clipboard" not in shot.context
    assert shot.notes == ["CLIPBOARD SKIPPED: LOOKS LIKE A SECRET"]

    same = desktop_answers(
        **{"wl-paste --no-newline --type text/plain;charset=utf-8": "TypeError: x is None"}
    )
    assert "clipboard" not in capture.screen_context(fake_session(same)[0]).context

    fallback = desktop_answers(**{f"grim -g {GEOMETRY} -": Result(1), "grim -o DP-2 -": PNG})
    assert capture.screen_context(fake_session(fallback)[0]).notes == [
        "CAPTURED THE FOCUSED OUTPUT"
    ]


@pytest.mark.anyio
async def test_screen_context_async():
    session, _ = fake_session(desktop_answers())
    shot = await capture.screen_context_async(session, image=False)
    assert shot.image is None and shot.context["app"] == "kitty"


# CLI --------------------------------------------------------------------------------------------


def run_cli(*argv: str) -> int:
    args = build_parser().parse_args(list(argv))
    return args.func(args)


def json_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def notes(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(cmd, "send_note", lambda title, body, level="info": sent.append((title, body, level)))  # fmt: skip
    return sent


@pytest.fixture
def server(make_runtime, mock, monkeypatch):
    """A real Bagley server behind ``Client``, through the test client (no network)."""
    rt = make_runtime()
    with TestClient(create_app(rt), base_url="http://localhost") as http:

        def client() -> Client:
            c = Client(url="http://localhost", token="")
            c.http.close()
            c.http = http
            return c

        monkeypatch.setattr(cmd, "Client", client)
        yield rt


def scripted(monkeypatch, events: list[dict[str, Any]], *, local: bool = False) -> list[dict]:
    """Replace the server with a fixed list of events; returns the bodies that were sent."""
    bodies: list[dict] = []

    def turn(body):
        bodies.append(body)
        return iter(events), local

    monkeypatch.setattr(cmd, "turn", turn)
    return bodies


def test_overlay_ask_streams_json_lines(server, mock, capsys, notes):
    mock.script = [Reply(text="Mount it with sshfs.")]
    assert run_cli("overlay-ask", "--json", "--", "how do I mount a remote folder?") == 0
    events = json_lines(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert types[-1] == "run.end" and {"run.start", "model", "text.delta"} <= set(types)
    start = next(e for e in events if e["type"] == "run.start")
    assert "".join(e["text"] for e in events if e["type"] == "text.delta") == "Mount it with sshfs."
    model = next(e for e in events if e["type"] == "model")
    assert model["machine"] and model["model"] == "qwen3:8b"
    conv = server.store.get_conversation(start["conversation_id"])
    assert conv is not None
    user = next(m for m in server.store.list_messages(conv["id"]) if m["role"] == "user")
    assert user["content"] == "how do I mount a remote folder?"
    assert notes == []  # Only with --notify.

    mock.script = [Reply(text="Use -o reconnect.")]
    cid = start["conversation_id"]
    assert run_cli("overlay-ask", "--json", "--notify", "-c", cid, "--", "and reconnect?") == 0
    again = json_lines(capsys.readouterr().out)
    assert next(e for e in again if e["type"] == "run.start")["conversation_id"] == cid
    assert notes and notes[0][0].startswith("BAGLEY // ") and notes[0][1] == "Use -o reconnect."


def test_see_sends_the_screen_to_a_vision_model(server, mock, capsys, notes, monkeypatch):
    mock.models["qwen2.5vl:7b"] = {
        "capabilities": ["completion", "vision"],
        "size": 6_000_000_000,
        "params": "8.3B",
        "family": "qwen25vl",
    }
    mock.script = [Reply(text="The **test** failed: `x` is None. Check the fixture.")]
    session, fake = fake_session(desktop_answers())
    monkeypatch.setattr(cmd, "desktop_session", lambda: session)

    assert run_cli("see", "--json") == 0
    events = json_lines(capsys.readouterr().out)
    assert events[0] == {
        "type": "context",
        "image": True,
        "image_bytes": len(PNG),
        "window_title": "~/code/app - pytest",
        "app": "kitty",
        "selection_chars": len("TypeError: x is None"),
        "clipboard_chars": len("pip install -e ."),
        "notes": [],
    }
    model = next(e for e in events if e["type"] == "model")
    assert model["model"] == "qwen2.5vl:7b"
    sent = mock.requests[-1]["messages"][-1]
    assert sent["images"] == [base64.b64encode(PNG).decode()]
    assert sent["content"].startswith(cmd.DEFAULT_SEE)
    assert "Focused window: ~/code/app - pytest" in sent["content"]
    assert "Selected text: TypeError: x is None" in sent["content"]
    assert "Clipboard: pip install -e ." in sent["content"]
    assert notes == [
        (f"BAGLEY // {model['machine'].upper()}", "The test failed: x is None. Check the fixture.", "info")
    ]  # fmt: skip
    assert fake.ran("grim") == [["grim", "-g", GEOMETRY, "-"]]


def test_see_without_an_image_or_anything_else(monkeypatch, capsys, notes):
    bodies = scripted(monkeypatch, [{"type": "text.delta", "text": "Looks fine."}])
    session, fake = fake_session(desktop_answers())
    monkeypatch.setattr(cmd, "desktop_session", lambda: session)
    assert run_cli("see", "--no-image", "--no-notify", "what", "is", "this?") == 0
    assert capsys.readouterr().out == "Looks fine.\n"
    assert bodies[0]["text"] == "what is this?" and "images" not in bodies[0]
    assert bodies[0]["context"]["selection"] == "TypeError: x is None"
    assert not fake.ran("grim") and notes == []

    session, _ = fake_session({}, env={})
    monkeypatch.setattr(cmd, "desktop_session", lambda: session)
    assert run_cli("see", "--json") == 1
    events = json_lines(capsys.readouterr().out)
    assert events[0]["notes"] == ["HYPRLAND NOT RUNNING", "NO WAYLAND SESSION"]
    assert events[1]["type"] == "error" and events[1]["message"].startswith("NOTHING TO SEE")
    assert len(bodies) == 1  # Nothing was asked.
    assert notes and notes[0][1].startswith("NOTHING TO SEE")


def test_overlay_ask_with_see(monkeypatch, capsys):
    bodies = scripted(monkeypatch, [])
    session, _ = fake_session(desktop_answers())
    monkeypatch.setattr(cmd, "desktop_session", lambda: session)
    assert run_cli("overlay-ask", "--json", "--see", "-c", "c0ffee", "--", "why red?") == 0
    assert json_lines(capsys.readouterr().out)[0]["type"] == "context"
    body = bodies[0]
    assert body["text"] == "why red?" and body["conversation_id"] == "c0ffee"
    assert body["source"] == "overlay" and body["approvals"] == "ask"
    assert body["images"] == [{"data": base64.b64encode(PNG).decode()}]
    assert body["context"]["app"] == "kitty"

    assert run_cli("overlay-ask", "--json") == 2  # No question and no --see.


def test_explain_selection_then_clipboard(monkeypatch, capsys, notes):
    bodies = scripted(
        monkeypatch,
        [{"type": "model", "machine": "h4ch1"}, {"type": "text.delta", "text": "It installs."}],
    )
    session, _ = fake_session(desktop_answers())
    monkeypatch.setattr(cmd, "desktop_session", lambda: session)
    assert run_cli("explain") == 0
    assert capsys.readouterr().out == "It installs.\n"
    assert bodies[-1]["text"].startswith("Explain the selected text")
    assert bodies[-1]["context"] == {
        "window_title": "~/code/app - pytest",
        "app": "kitty",
        "selection": "TypeError: x is None",
    }
    assert notes == [("BAGLEY // H4CH1", "It installs.", "info")]

    nothing_selected = desktop_answers(**{"wl-paste --primary --list-types": Result(1)})
    monkeypatch.setattr(cmd, "desktop_session", lambda: fake_session(nothing_selected)[0])
    assert run_cli("explain", "--json", "--no-notify") == 0
    assert bodies[-1]["text"].startswith("Explain the text in my clipboard")
    assert bodies[-1]["context"]["clipboard"] == "pip install -e ."
    assert json_lines(capsys.readouterr().out)[0]["clipboard_chars"] == len("pip install -e .")


def test_explain_with_nothing_selected(monkeypatch, capsys, notes):
    bodies = scripted(monkeypatch, [])
    empty = {"wl-paste --primary --list-types": Result(1), "wl-paste --list-types": "image/png"}
    monkeypatch.setattr(cmd, "desktop_session", lambda: fake_session(empty)[0])
    assert run_cli("explain", "--json") == 1
    error = json_lines(capsys.readouterr().out)[0]
    assert error == {
        "type": "error",
        "message": "NOTHING SELECTED. Highlight some text first.",
        "notes": ["CLIPBOARD NOT TEXT (image/png)"],
    }
    assert bodies == [] and notes == [("BAGLEY", error["message"], "info")]

    password = desktop_answers(
        **{
            "wl-paste --primary --list-types": Result(1),
            "wl-paste --no-newline --type text/plain;charset=utf-8": "Tr0ub4dor&3xQ",
        }
    )
    monkeypatch.setattr(cmd, "desktop_session", lambda: fake_session(password)[0])
    assert run_cli("explain", "--no-notify") == 1
    assert "LOOKS LIKE A SECRET" in capsys.readouterr().err and bodies == []


def test_approval_requests_carry_a_summary(monkeypatch, capsys):
    events = [
        {"type": "run.start", "conversation_id": "c1"},
        {
            "type": "tool.start",
            "call": {"id": "call_1", "name": "run_command", "arguments": {"command": "ls -la"}},
            "summary": "Run `{command}`",
            "risk": "confirm",
        },
        {"type": "status", "state": "approval"},
        {
            "type": "approval.request",
            "call": {"id": "call_1", "name": "run_command", "arguments": {"command": "ls -la"}},
            "description": "Run a shell command.",
        },
        {"type": "approval.result", "id": "call_1", "allowed": False},
        {"type": "run.end", "conversation_id": "c1"},
    ]
    scripted(monkeypatch, events)
    assert run_cli("overlay-ask", "--json", "list", "files") == 0
    request = next(
        e for e in json_lines(capsys.readouterr().out) if e["type"] == "approval.request"
    )
    assert request["summary"] == "Run `ls -la`" and request["call"]["id"] == "call_1"

    assert run_cli("overlay-ask", "list", "files") == 0
    err = capsys.readouterr().err
    assert "» EXEC run_command" in err
    assert "AWAIT // Run `ls -la` // bagley approve call_1" in err
    assert "[WARN] DENIED" in err


def test_terminal_output_is_plain_and_safe(monkeypatch, capsys, notes):
    events = [
        {"type": "text.delta", "text": "Checking."},
        {"type": "message"},
        {"type": "tool.start", "call": {"id": "1", "name": "web_search", "arguments": {}}},
        {"type": "text.delta", "text": "Done \x1b[31mred\x1b[0m."},
        {"type": "message"},
        {"type": "error", "message": "Model went away.", "hint": "Start Ollama."},
    ]
    scripted(monkeypatch, events)
    assert run_cli("overlay-ask", "--notify", "check") == 1
    out = capsys.readouterr()
    assert out.out == "Checking.\n\nDone  [31mred [0m.\n"
    assert "» EXEC web_search" in out.err and "[CRIT] Model went away." in out.err
    assert "Start Ollama." in out.err and "\x1b" not in out.err  # Not a TTY: no colour.
    assert notes == [("BAGLEY // ERROR", "Model went away.", "critical")]


def test_falls_back_to_this_process_without_a_server(monkeypatch, capsys):
    class Offline:
        def available(self) -> bool:
            return False

        def close(self) -> None:
            pass

    asked: list[dict] = []

    def local(body):
        asked.append(body)
        yield {"type": "text.delta", "text": "Local answer."}

    monkeypatch.setattr(cmd, "Client", Offline)
    monkeypatch.setattr(cmd, "ask_local", local)
    assert run_cli("overlay-ask", "--json", "hi") == 0
    events = json_lines(capsys.readouterr().out)
    assert events[0] == {"type": "notice", "message": "NO SERVER // RUNNING HERE"}
    assert events[1]["text"] == "Local answer."
    assert asked[0]["text"] == "hi" and asked[0]["source"] == "overlay"


def test_a_server_that_drops_mid_stream(monkeypatch, capsys):
    def broken():
        yield {"type": "text.delta", "text": "Half"}
        raise ServerUnavailable("connection reset")

    monkeypatch.setattr(cmd, "turn", lambda body: (broken(), False))
    assert run_cli("overlay-ask", "--json", "hi") == 1
    events = json_lines(capsys.readouterr().out)
    assert events[-1] == {"type": "error", "message": "NO SIGNAL: connection reset"}

    def refused():
        raise ServerUnavailable("413: Images up to 12 MB are accepted.")
        yield

    monkeypatch.setattr(cmd, "turn", lambda body: (refused(), False))
    assert run_cli("overlay-ask", "hi") == 1
    assert "[CRIT] 413: Images up to 12 MB are accepted." in capsys.readouterr().err


def test_plain_notification_text():
    reply = "## Fix\nRun **this**:\n```bash\nrm -rf build\n```\nthen `make`."
    assert cmd.plain(reply) == "Fix Run this: [code] then make."
    assert len(cmd.plain("word " * 300)) == 500
