"""Load tools on demand so small local models aren't handed every tool on every request.

Core tools (time, maths, web, reading files, memory, knowledge, plugins, MCP) are always sent.
The other groups join when the conversation calls for them: a keyword hint in recent messages,
earlier use in the same chat, or the model asking for them through the ``load_tools`` tool.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from bagley.tools import Tool

GROUPS: dict[str, tuple[str, str]] = {
    "files": (
        "create, edit, move and delete files in the workspace (every change can be reverted)",
        r"\b(write|save|creat|edit|chang|renam|move|delet|remov|organi[sz]|folder|file|note|append|updat|fix|replac|tidy|clean|draft)",
    ),
    "automation": (
        "reminders, scheduled tasks and web page watchers that run later on their own",
        r"\b(remind|schedul|every|daily|weekly|tomorrow|later|tonight|morning|evening|watch|monitor|track|notif|alert|automat|cancel|briefing|recurring)|\bat \d|\bin \d+ ?(min|hour|day)",
    ),
    "system": (
        "this computer: CPU, memory, disk, battery and processes; open pages or files; notifications",
        r"\b(cpu|ram|memory usage|slow|battery|disk|storage|process|laptop|computer|machine|system|open|launch|notif|uptime|fan|hot)",
    ),
    "code": (
        "run Python code and shell commands, e.g. to analyse data or make charts",
        r"\b(run|python|code|script|execut|command|terminal|shell|plot|chart|graph|csv|excel|analy[sz]|install|pip|git)",
    ),
}

ROUTING_THRESHOLD = 14  # Below this many tools, just send them all.
FEATURE_GROUPS: set[str] = set()


def register_group(name: str, description: str, pattern: str) -> None:
    """Let a feature module's tools load on demand: tools whose category is ``name`` join when
    a recent message matches ``pattern`` or the model asks for the group."""
    GROUPS[name] = (description, pattern)
    FEATURE_GROUPS.add(name)


def group_of(tool: Tool) -> str:
    if tool.source != "builtin":
        return "core"  # Plugins and MCP servers were added deliberately.
    if tool.name in ("run_command", "run_python"):
        return "code"
    if tool.category == "automation":
        return "automation"
    if tool.category == "files" and (tool.risk == "confirm" or tool.name == "make_directory"):
        return "files"
    if tool.category == "system":
        return "system"
    if tool.category in FEATURE_GROUPS:
        return tool.category
    return "core"


def _loader(groups: list[str]) -> Tool:
    async def load(groups: list[str]) -> str:
        known = [g for g in groups if g in GROUPS]
        if not known:
            return f"Unknown group. Choose from: {', '.join(GROUPS)}."
        return f"Loaded {', '.join(known)} tools. They are available from your next step."

    listing = "; ".join(f"{g}: {GROUPS[g][0]}" for g in groups)
    return Tool(
        name="load_tools",
        description=f"Load more tools when you need them. Available groups: {listing}.",
        parameters={
            "type": "object",
            "properties": {
                "groups": {"type": "array", "items": {"type": "string", "enum": groups}}
            },
            "required": ["groups"],
        },
        func=load,
        category="general",
        summary="Load {groups} tools",
    )


def select_tools(
    tools: list[Tool], history: Iterable[dict[str, Any]], force: Iterable[str] = ()
) -> list[Tool]:
    """Pick the tools to offer for the next step of a conversation. ``force`` names groups
    the conversation's mode always needs."""
    if len(tools) <= ROUTING_THRESHOLD:
        return tools
    by_name = {t.name: t for t in tools}
    active = {"core", *force}
    users: list[str] = []
    for m in history:
        if m["role"] == "user":
            users.append(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            if fn.get("name") == "load_tools":
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    args = {"groups": re.findall(r"\w+", args)}
                active.update(g for g in args.get("groups", []) if g in GROUPS)
            elif fn.get("name") in by_name:
                active.add(group_of(by_name[fn["name"]]))
    recent = " ".join(users[-2:]).lower()
    letters = [c for c in recent if c.isalpha()]
    if letters and sum(c.isascii() for c in letters) / len(letters) < 0.6:
        return (
            tools  # Keywords only cover Latin-script text; don't hide tools from other languages.
        )
    for group, (_, pattern) in GROUPS.items():
        if re.search(pattern, recent):
            active.add(group)
    selected = [t for t in tools if group_of(t) in active]
    missing = sorted({group_of(t) for t in tools} - active)
    if missing:
        selected.append(_loader(missing))
    return selected
