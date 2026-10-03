import io

import pytest

from bagley import cli
from tests.demo_stack import free_port, serve_in_thread
from tests.mock_llm import MockLLM, Reply


@pytest.fixture
def ask(tmp_path, monkeypatch):
    mock = MockLLM()
    port = free_port()
    server = serve_in_thread(mock.app, port)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BAGLEY_PROVIDER", "ollama")
    monkeypatch.setenv("BAGLEY_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("BAGLEY_MODEL", "qwen3:8b")

    def run(*argv: str, stdin: str = "") -> int:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        return cli.main(["ask", *argv])

    run.mock = mock
    run.workspace = tmp_path / "data" / "workspace"
    yield run
    server.should_exit = True


def test_ask_prints_the_answer_and_attaches_piped_input(ask, capsys):
    ask.mock.script = [Reply(text="Two errors, both timeouts.")]
    assert ask("summarise", "this", stdin="ERROR timeout\nERROR timeout\n") == 0
    out = capsys.readouterr().out
    assert out == "Two errors, both timeouts.\n"
    sent = ask.mock.requests[0]["messages"][-1]["content"]
    assert sent.startswith("summarise this") and "<input>\nERROR timeout" in sent


def test_ask_needs_yes_for_risky_tools(ask, capsys):
    note = Reply(tool_calls=[("write_file", {"path": "todo.md", "content": "milk"})])
    ask.mock.script = [note, Reply(text="Could not save it.")]
    assert ask("save a todo") == 0
    captured = capsys.readouterr()
    assert "skipped write_file" in captured.err and not (ask.workspace / "todo.md").exists()
    assert captured.out == "Could not save it.\n"

    ask.mock.script = [note, Reply(text="Saved.")]
    assert ask("-y", "-q", "save a todo") == 0
    captured = capsys.readouterr()
    assert (ask.workspace / "todo.md").read_text() == "milk" and captured.err == ""


def test_ask_without_a_question(ask, capsys):
    assert ask() == 2
    assert "Usage" in capsys.readouterr().err
