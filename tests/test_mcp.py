from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bagley.mcp import McpManager
from bagley.tools import Registry, ToolError

pytestmark = pytest.mark.anyio

SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


async def test_mcp_tools_are_registered_and_callable(tmp_path, make_runtime):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {"command": sys.executable, "args": [SERVER]},
                    "trusted": {"command": sys.executable, "args": [SERVER], "trust": True},
                    "missing": {"command": "definitely-not-a-real-binary-xyz"},
                    "off": {"command": "x", "disabled": True},
                }
            }
        )
    )
    registry = Registry()
    manager = McpManager(config, registry)
    await manager.start()
    try:
        status = {s["name"]: s for s in manager.status()}
        assert status["fake"]["state"] == "running" and status["fake"]["tools"] == 2
        assert status["missing"]["state"] == "error"
        assert "off" not in status

        echo = registry.get("fake__echo")
        assert echo.risk == "confirm" and echo.category == "mcp"
        assert registry.get("trusted__echo").risk == "safe"
        rt = make_runtime()
        ctx = rt.tool_context()
        assert await echo.invoke({"text": "hi"}, ctx) == "echo: hi"
        with pytest.raises(ToolError, match="it broke"):
            await registry.get("fake__fail").invoke({}, ctx)
    finally:
        await manager.stop()


async def test_invalid_config_is_reported(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text("{not json")
    registry = Registry()
    await McpManager(config, registry).start()
    assert registry.errors[0]["source"] == "mcp"
