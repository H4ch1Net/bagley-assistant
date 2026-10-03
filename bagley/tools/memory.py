"""Long-term memory. Saved facts are added to the system prompt of every conversation."""

from __future__ import annotations

from typing import Annotated

from bagley.tools import ToolContext, ToolError, tool


@tool(category="memory", summary="Remember “{fact}”")
def remember(
    ctx: ToolContext,
    fact: Annotated[str, "A short, self-contained fact about the user or their preferences"],
) -> str:
    """Save a lasting fact about the user (name, preferences, projects) for future conversations.
    Only use it for things worth remembering, not for the current task."""
    if len(fact.strip()) < 3:
        raise ToolError("The fact is empty.")
    item = ctx.store.add_memory(fact)
    return f"Saved memory #{item['id']}: {item['content']}"


@tool(category="memory", summary="Forget memory #{memory_id}")
def forget(
    ctx: ToolContext,
    memory_id: Annotated[int, "The number of the memory to delete, as shown in your memory list"],
) -> str:
    """Delete a saved memory when the user asks you to forget something or it is out of date."""
    if not ctx.store.delete_memory(memory_id):
        raise ToolError(f"There is no memory #{memory_id}.")
    return f"Forgot memory #{memory_id}."
