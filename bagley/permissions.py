"""Permission tiers: whether a tool runs on its own, asks first, or is switched off.

Every tool has a default from its risk (tools that change things ask, the rest are allowed).
``Preferences.tool_permissions`` overrides it per tool. Runs nobody is watching (automations)
never use tools whose default is to ask, whatever the tier says.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bagley.config import Preferences
    from bagley.tools import Tool

TIERS = ("allow", "ask", "deny")


def default_permission(tool: Tool) -> str:
    return "ask" if tool.risk == "confirm" else "allow"


def permission_for(tool: Tool, prefs: Preferences) -> str:
    tier = prefs.tool_permissions.get(tool.name)
    if tier in TIERS:
        return tier
    if tool.name in prefs.disabled_tools:
        return "deny"
    return default_permission(tool)


def denied(prefs: Preferences) -> list[str]:
    """Names of tools that must not be offered: switched off or set to deny."""
    names = set(prefs.disabled_tools)
    names |= {name for name, tier in prefs.tool_permissions.items() if tier == "deny"}
    names -= {name for name, tier in prefs.tool_permissions.items() if tier != "deny"}
    return sorted(names)
