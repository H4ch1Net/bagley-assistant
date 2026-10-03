from __future__ import annotations

import os
from typing import Annotated, Literal

import httpx
import pytest

from bagley.config import ServerConfig
from bagley.store import Store
from bagley.tools import Registry, ToolContext, ToolError, build_registry, tool
from bagley.tools.core import safe_eval
from bagley.tools.web import check_url, html_to_text, parse_duckduckgo
from tests.mock_llm import DDG_HTML, fake_web_transport


@pytest.fixture
def ctx(config: ServerConfig):
    config.ensure_dirs()
    store = Store(":memory:")
    client = httpx.AsyncClient(transport=fake_web_transport())
    yield ToolContext(config=config, store=store, http=client)
    store.close()


def test_schema_from_signature():
    @tool(summary="x")
    def sample(
        name: Annotated[str, "Who"],
        count: int = 2,
        mode: Literal["a", "b"] = "a",
        tags: list[str] | None = None,
    ) -> str:
        """Do a thing."""
        return name

    params = sample.parameters
    assert sample.description == "Do a thing."
    assert params["required"] == ["name"]
    assert params["properties"]["name"] == {"type": "string", "description": "Who"}
    assert params["properties"]["count"] == {"type": "integer", "default": 2}
    assert params["properties"]["mode"]["enum"] == ["a", "b"]
    assert params["properties"]["tags"]["type"] == "array"


@pytest.mark.anyio
async def test_coercion_and_validation(ctx):
    @tool()
    def add(a: int, b: float = 1.0, flag: bool = False, mode: Literal["x", "y"] = "x") -> dict:
        return {"sum": a + b, "flag": flag, "mode": mode}

    assert await add.invoke({"a": "2", "b": "0.5", "flag": "true", "extra": 1}, ctx) == (
        '{"sum": 2.5, "flag": true, "mode": "x"}'
    )
    with pytest.raises(ToolError, match="Missing required"):
        await add.invoke({}, ctx)
    with pytest.raises(ToolError, match="must be one of"):
        await add.invoke({"a": 1, "mode": "z"}, ctx)
    with pytest.raises(ToolError, match="should be of type integer"):
        await add.invoke({"a": "lots"}, ctx)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("2+2", 4),
        ("2^10", 1024),
        ("1,234 * 2", 2468),
        ("max(1, 5)", 5),
        ("round(pi, 2)", 3.14),
        ("7/2", 3.5),
    ],
)
def test_calculator(expr, expected):
    assert safe_eval(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os')",
        "1/0",
        "9**9**9",
        "().__class__",
        "x + 1",
        "perm(1500000)",
        "comb(10**7, 5000)",
    ],
)
def test_calculator_rejects(expr):
    with pytest.raises(ToolError):
        safe_eval(expr)


@pytest.mark.anyio
async def test_file_tools_round_trip(ctx):
    reg = build_registry(ctx.config)
    await reg.get("write_file").invoke({"path": "notes/a.md", "content": "hello\nworld"}, ctx)
    listing = await reg.get("list_files").invoke({"path": "notes"}, ctx)
    assert "a.md" in listing
    read = await reg.get("read_file").invoke({"path": "notes/a.md"}, ctx)
    assert "hello" in read
    found = await reg.get("search_files").invoke({"query": "WORLD"}, ctx)
    assert '"line": 2' in found


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["../outside.txt", "notes/../../x", "/../../etc/passwd"])
async def test_file_tools_stay_in_workspace(ctx, path):
    reg = build_registry(ctx.config)
    with pytest.raises(ToolError, match="outside the workspace"):
        await reg.get("write_file").invoke({"path": path, "content": "x"}, ctx)


@pytest.mark.anyio
@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_file_tools_reject_symlink_escape(ctx, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    (ctx.config.workspace / "link.txt").symlink_to(secret)
    with pytest.raises(ToolError, match="outside the workspace"):
        await build_registry(ctx.config).get("read_file").invoke({"path": "link.txt"}, ctx)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8765/api", "http://localhost/", "http://10.0.0.1/", "file:///etc/passwd"],
)
async def test_private_urls_blocked(url):
    with pytest.raises(ToolError):
        await check_url(url, allow_private=False)


@pytest.mark.anyio
async def test_weather_tool(ctx):
    result = (
        await build_registry(ctx.config).get("get_weather").invoke({"location": "Lisbon, PT"}, ctx)
    )
    assert "Lisbon, Lisbon, Portugal" in result
    assert "mainly clear" in result


@pytest.mark.anyio
async def test_fetch_pins_the_validated_ip(ctx):
    from tests.mock_llm import WEB_REQUESTS

    fetch = build_registry(ctx.config).get("fetch_webpage")
    out = await fetch.invoke({"url": "https://93.184.215.14:8443/page"}, ctx)
    sent = WEB_REQUESTS[-1]
    assert sent.url.host == "93.184.215.14"
    assert sent.headers["host"] == "93.184.215.14:8443"
    assert sent.extensions["sni_hostname"] == "93.184.215.14"
    assert '"title": "Example"' in out and "https://93.184.215.14:8443/page" in out

    with pytest.raises(ToolError, match="private or local"):
        await fetch.invoke({"url": "http://93.184.215.14/to-private"}, ctx)
    with pytest.raises(ToolError, match="too large"):
        await fetch.invoke({"url": "http://93.184.215.14/huge"}, ctx)


def test_duckduckgo_parser():
    results = parse_duckduckgo(DDG_HTML, 5)
    assert results[0] == {
        "title": "First result",
        "url": "https://example.com/one",
        "snippet": "Snippet & one",
    }
    assert results[1]["url"] == "https://example.org/two"


def test_html_to_text():
    title, text = html_to_text(
        "<html><head><title> Hi </title><style>x{}</style></head><body><nav>menu</nav>"
        "<h1>Head</h1><p>One &amp; two</p><ul><li>a</li><li>b</li></ul><script>bad()</script></body></html>"
    )
    assert title == "Hi"
    assert text == "# Head\n\nOne & two\n\n- a\n\n- b"


@pytest.mark.anyio
async def test_memory_tools(ctx):
    reg = build_registry(ctx.config)
    out = await reg.get("remember").invoke({"fact": "Likes tea"}, ctx)
    assert "Saved memory #1" in out
    await reg.get("remember").invoke({"fact": "likes tea"}, ctx)  # Deduplicated.
    assert len(ctx.store.list_memories()) == 1
    await reg.get("forget").invoke({"memory_id": 1}, ctx)
    with pytest.raises(ToolError):
        await reg.get("forget").invoke({"memory_id": 1}, ctx)


def test_shell_only_when_enabled(config):
    assert build_registry(config).get("run_command") is None
    config.enable_shell = True
    assert build_registry(config).get("run_command").risk == "confirm"


@pytest.mark.anyio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
async def test_shell_timeout_kills_child_processes(ctx):
    import time

    ctx.config.enable_shell = True
    run = build_registry(ctx.config).get("run_command")
    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out"):
        await run.invoke({"command": "sleep 30; echo done", "timeout": 1}, ctx)
    assert time.monotonic() - started < 10


@pytest.mark.anyio
async def test_shell_runs_in_workspace(ctx):
    ctx.config.enable_shell = True
    run = build_registry(ctx.config).get("run_command")
    out = await run.invoke({"command": "echo hi"}, ctx)
    assert '"exit_code": 0' in out and "hi" in out


def test_plugins_load_and_errors_are_reported(config):
    config.ensure_dirs()
    (config.plugins_dir / "dice.py").write_text(
        "from bagley.tools import tool\n\n@tool()\ndef roll(sides: int = 6) -> int:\n    'Roll.'\n    return 4\n"
    )
    (config.plugins_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    reg = build_registry(config)
    assert reg.get("roll").source == "plugin:dice.py"
    assert reg.errors == [{"source": "plugin:broken.py", "error": "boom"}]


@pytest.mark.anyio
async def test_example_plugin(config, ctx):
    from pathlib import Path

    config.plugins_dir = Path(__file__).parents[1] / "examples" / "plugins"
    reg = build_registry(config)
    assert reg.errors == []
    assert '"total"' in await reg.get("roll_dice").invoke({"sides": "6", "count": 2}, ctx)
    assert "free_gb" in await reg.get("disk_space").invoke({}, ctx)


def test_registry_enabled_filter():
    reg = Registry()

    @tool()
    def a() -> str:
        return "a"

    reg.add(a)
    assert reg.enabled(["a"]) == []
    assert reg.enabled() == [a]


@pytest.mark.anyio
async def test_system_status(ctx):
    import json

    data = json.loads(await build_registry(ctx.config).get("system_status").invoke({"top": 3}, ctx))
    assert 0 <= data["cpu_percent"] <= 100 * data["cpu_cores"]
    assert data["memory"]["total_gb"] > 0
    assert len(data["top_memory"]) <= 3


@pytest.mark.anyio
async def test_open_on_computer(ctx, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    tool = build_registry(ctx.config).get("open_on_computer")
    assert tool.risk == "confirm"
    assert "Opened" in await tool.invoke({"target": "https://example.com"}, ctx)
    assert opened == ["https://example.com"]
    with pytest.raises(ToolError, match="Only http"):
        await tool.invoke({"target": "file:///etc/passwd"}, ctx)
    with pytest.raises(ToolError, match="does not exist"):
        await tool.invoke({"target": "nope.txt"}, ctx)
