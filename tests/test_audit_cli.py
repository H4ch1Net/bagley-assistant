from __future__ import annotations

import json
from datetime import datetime

import pytest

from bagley import cli
from bagley.commands import audit as audit_cmd
from bagley.commands.audit import Paint, format_entry
from bagley.server import create_app
from bagley.store import Store
from tests.demo_stack import free_port, serve_in_thread
from tests.mock_llm import MockLLM

WHEN = datetime(2026, 10, 7, 16, 43, 7).timestamp()

ROWS = [
    {
        "tool": "run_command",
        "permission": "ask",
        "decision": "approved",
        "ok": True,
        "duration_ms": 412,
        "sandboxed": True,
        "arguments": {"command": "ls -la"},
    },
    {
        "tool": "write_file",
        "permission": "ask",
        "decision": "denied",
        "source": "phone",
        "arguments": {"path": "notes/x.md"},
        "conversation_id": "c2",
    },
    {
        "tool": "web_search",
        "permission": "allow",
        "decision": "auto",
        "ok": False,
        "duration_ms": 1234,
        "arguments": {"query": "lisbon weather"},
        "detail": "Error: timed out",
    },
]


def entry(**fields):
    return {"id": 1, "created_at": WHEN, "source": "web", "ok": None, "sandboxed": False, **fields}


def test_format_entry_reads_like_a_ctos_log():
    plain = Paint(on=False)
    assert format_entry(entry(**ROWS[0]), plain) == (
        '071026-1643:07  EXEC  run_command   ASK>APPROVED  OK    412ms  BWRAP  {"command": "ls -la"}'
    )
    assert format_entry(entry(**ROWS[1]), plain) == (
        '071026-1643:07  EXEC  write_file    ASK>DENIED    --           phone  {"path": "notes/x.md"}'
    )
    assert format_entry(entry(**ROWS[2]), plain) == (
        '071026-1643:07  EXEC  web_search    AUTO          FAIL  1.2s          {"query": "lisbon weather"}'
    )
    blocked = entry(tool="run_command", permission="ask", decision="blocked", source="automation")
    assert "ASK>BLOCKED   --" in format_entry(blocked, plain) and "automation" in format_entry(
        blocked, plain
    )
    missing = entry(tool="nope", permission="deny", decision="unknown")
    assert format_entry(missing, plain).split()[3] == "DENY"


def test_format_entry_colors_and_width():
    color = Paint(on=True)
    line = format_entry(entry(**ROWS[0]), color)
    assert "\033[38;2;122;122;122m071026-1643:07" in line  # Gray date.
    assert "\033[38;2;255;255;255mrun_command" in line  # White tool.
    assert "\033[38;2;0;250;154mOK" in line
    assert "\033[38;2;252;62;56mFAIL" in format_entry(entry(**ROWS[2]), color)
    long = entry(**{**ROWS[0], "arguments": {"command": "x" * 300}})
    cut = format_entry(long, Paint(on=False), width=100)
    assert len(cut) == 100 and cut.endswith("…")


def test_paint_respects_no_color(monkeypatch):
    class Tty:
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert Paint(Tty()).on
    monkeypatch.setenv("NO_COLOR", "1")
    assert not Paint(Tty()).on
    monkeypatch.delenv("NO_COLOR")
    assert not Paint(object()).on  # Not a terminal.


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """No Bagley server: commands work on the database in BAGLEY_DATA_DIR."""
    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BAGLEY_URL", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("NO_COLOR", "1")
    return tmp_path / "data" / "bagley.db"


def seed(store: Store) -> None:
    for row in ROWS:
        store.add_audit(**row)


def test_audit_reads_the_database_without_a_server(offline, capsys, tmp_path):
    assert cli.main(["audit"]) == 0
    assert "NO ENTRIES" in capsys.readouterr().out and not offline.exists()

    store = Store(offline)
    seed(store)
    store.close()
    assert cli.main(["audit", "-n", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[2] for line in lines] == ["write_file", "web_search"]  # Oldest first.

    assert cli.main(["audit", "--tool", "run_command", "--json"]) == 0
    [row] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert row["tool"] == "run_command" and row["sandboxed"] is True and row["ok"] is True

    assert cli.main(["audit", "--conversation", "c2"]) == 0
    assert capsys.readouterr().out.split()[2] == "write_file"

    target = tmp_path / "audit.jsonl"
    assert cli.main(["audit", "--export", str(target)]) == 0
    assert "[OK] EXPORTED 3 ENTRIES" in capsys.readouterr().out
    exported = [json.loads(line) for line in target.read_text().splitlines()]
    assert [r["tool"] for r in exported] == ["run_command", "write_file", "web_search"]


@pytest.fixture
def server(make_runtime, monkeypatch):
    rt = make_runtime()
    port = free_port()
    app = serve_in_thread(create_app(rt), port)
    monkeypatch.setenv("BAGLEY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NO_COLOR", "1")
    yield rt
    app.should_exit = True


def test_audit_follows_the_server(server, capsys, monkeypatch):
    seed(server.store)
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 1:
            server.store.add_audit(tool="get_weather", permission="allow", decision="auto", ok=True)
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(audit_cmd.time, "sleep", nap)
    assert cli.main(["audit", "-f", "-n", "1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[2] for line in lines] == ["web_search", "get_weather"]
    assert naps == [2.0, 2.0]


# bagley machines -----------------------------------------------------------------------------------


@pytest.fixture
def model_server(offline, monkeypatch):
    mock = MockLLM()
    port = free_port()
    app = serve_in_thread(mock.app, port)
    url = f"http://127.0.0.1:{port}"
    store = Store(offline)
    store.set_preferences({"provider": "ollama", "base_url": url, "machine_name": "b1t"})
    store.close()
    yield url
    app.should_exit = True


def test_machines_without_a_server(model_server, offline, capsys, monkeypatch):
    monkeypatch.setenv("DESK_KEY", "sk-secret")
    assert cli.main(["machines", "add", "H4CH1 Desk", model_server, "--provider", "ollama",
                     "--model", "qwen3:8b", "--key-env", "DESK_KEY"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "[OK] MACHINE h4ch1-desk ADDED  GPU" in out and "sk-secret" not in out
    store = Store(offline)
    [saved] = store.get_preferences()["machines"]
    store.close()
    assert saved["id"] == "h4ch1-desk" and saved["api_key"] == "sk-secret"
    assert saved["base_url"] == model_server and saved["model"] == "qwen3:8b"

    assert cli.main(["machines", "add", "H4CH1 Desk", model_server]) == 1
    assert "already exists" in capsys.readouterr().err
    monkeypatch.delenv("DESK_KEY")
    assert cli.main(["machines", "add", "Other", model_server, "--key-env", "DESK_KEY"]) == 2
    assert "DESK_KEY is not set" in capsys.readouterr().err

    assert cli.main(["machines", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["routing"] == "auto"
    local, desk = data["machines"]  # In configuration order, with their priority.
    assert (desk["id"], desk["ok"], desk["priority"], desk["base_url"]) == (
        "h4ch1-desk",
        True,
        0,
        model_server,
    )
    assert local["id"] == "local" and local["name"] == "B1T" and len(local["models"]) == 3
    assert "api_key" not in json.dumps(data)

    assert cli.main(["machines"]) == 0
    table = capsys.readouterr().out.splitlines()
    assert table[0] == "ROUTING AUTO // LIGHT LOCAL"
    assert table[2].startswith("01  h4ch1-desk  H4CH1 DESK  GPU") and "ONLINE" in table[2]
    assert table[3].startswith("02  local") and table[3].split()[-2] == "ONLINE"

    assert cli.main(["machines", "route"]) == 0
    assert capsys.readouterr().out.strip() == "CHAT -> H4CH1 DESK // GPU // qwen3:8b"
    assert cli.main(["machines", "route", "--purpose", "light", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["machine_id"] == "local"

    assert cli.main(["machines", "remove", "h4ch1-desk"]) == 0
    assert "[OK] MACHINE h4ch1-desk REMOVED" in capsys.readouterr().out
    assert cli.main(["machines", "remove", "h4ch1-desk"]) == 1
    assert cli.main(["machines", "remove", "local"]) == 2


def test_machines_add_a_hosted_api_by_preset(offline, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(["machines", "add", "Claude", "--preset", "claude"]) == 0
    out = capsys.readouterr().out
    assert "[OK] MACHINE claude ADDED  CLOUD  https://api.anthropic.com" in out
    assert "Set ANTHROPIC_API_KEY" in out
    store = Store(offline)
    [saved] = store.get_preferences()["machines"]
    store.close()
    assert (saved["provider"], saved["model"]) == ("anthropic", "claude-opus-5-5")
    assert cli.main(["machines", "add", "Nowhere"]) == 2
    assert "--preset" in capsys.readouterr().err


def test_machines_through_the_server(server, capsys, monkeypatch):
    monkeypatch.setenv("CLOUD_KEY", "sk-cloud")
    argv = ["machines", "add", "Cloud", "http://mock2/v1", "--role", "cloud"]
    assert (
        cli.main([*argv, "--provider", "openai", "--key-env", "CLOUD_KEY", "--model", "qwen3:8b"])
        == 0
    )
    capsys.readouterr()
    prefs, _ = server.preferences()
    assert prefs.machines[0].api_key == "sk-cloud" and prefs.machines[0].role == "cloud"

    server.update_preferences({"routing": "cloud"})
    assert cli.main(["machines", "--json", "--fresh"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["routing"] == "cloud"
    assert {m["id"]: m["base_url"] for m in data["machines"]} == {
        "local": "http://mock",
        "cloud": "http://mock2/v1",
    }
    assert cli.main(["machines", "route", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["machine_id"] == "cloud"

    assert cli.main(["machines", "remove", "cloud"]) == 0
    prefs, _ = server.preferences()
    assert prefs.machines == [] and prefs.routing == "auto"  # No longer pinned to it.
