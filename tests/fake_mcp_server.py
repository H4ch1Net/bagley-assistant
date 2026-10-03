"""Tiny stdio MCP server used by tests/test_mcp.py."""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "echo":
            result = {
                "content": [{"type": "text", "text": "echo: " + msg["params"]["arguments"]["text"]}]
            }
        else:
            result = {"content": [{"type": "text", "text": "it broke"}], "isError": True}
    else:
        print(
            json.dumps(
                {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}
            ),
            flush=True,
        )
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
