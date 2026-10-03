"""Example Bagley plugin. Copy this file into ~/.bagley/plugins/ and restart Bagley.

Any function decorated with @tool becomes available to the model. Type hints become the
JSON schema, Annotated strings become parameter descriptions, and the docstring is the
tool description the model reads when deciding what to call.
"""

from __future__ import annotations

import random
import shutil
from typing import Annotated

from bagley.tools import ToolContext, ToolError, tool


@tool(category="utility", summary="Roll {count}d{sides}")
def roll_dice(
    sides: Annotated[int, "Faces per die"] = 6,
    count: Annotated[int, "How many dice to roll (1-20)"] = 1,
) -> dict:
    """Roll dice and return each result and the total."""
    if not 2 <= sides <= 1000 or not 1 <= count <= 20:
        raise ToolError("Use 2-1000 sides and 1-20 dice.")
    rolls = [random.randint(1, sides) for _ in range(count)]
    return {"rolls": rolls, "total": sum(rolls)}


@tool(category="system", summary="Check free disk space")
def disk_space(ctx: ToolContext) -> dict:
    """Report total, used and free disk space for the drive holding the workspace."""
    usage = shutil.disk_usage(ctx.config.workspace)
    gb = 1024**3
    return {
        "total_gb": round(usage.total / gb, 1),
        "used_gb": round(usage.used / gb, 1),
        "free_gb": round(usage.free / gb, 1),
    }
