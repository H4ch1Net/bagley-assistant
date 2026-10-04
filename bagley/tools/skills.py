"""Skills: saved procedures Bagley can read, write and improve."""

from __future__ import annotations

from typing import Annotated

from bagley.skills import SkillError, SkillStore
from bagley.tools import ToolContext, ToolError, tool


def _store(ctx: ToolContext) -> SkillStore:
    if ctx.runtime is None:
        raise ToolError("Skills are not available here.")
    return ctx.runtime.skills


@tool(category="skills", summary="Read skill “{name}”")
def read_skill(
    ctx: ToolContext,
    name: Annotated[str, "The skill's name, as listed under Skills in your instructions"],
) -> str:
    """Load the full instructions of a saved skill. Do this first whenever a listed skill matches
    the request, then follow it."""
    store = _store(ctx)
    skill = store.get(name)
    if skill is None:
        names = ", ".join(s.name for s in store.all()) or "none yet"
        raise ToolError(f"There is no skill named '{name}'. Skills: {names}.")
    store.touch(skill.name)
    return skill.text()


@tool(category="skills", risk="confirm", summary="Save skill “{name}”")
async def save_skill(
    ctx: ToolContext,
    name: Annotated[str, "Short lowercase name with hyphens, e.g. 'weekly-report'"],
    description: Annotated[str, "One sentence: what the skill does and when to use it"],
    instructions: Annotated[str, "Numbered Markdown steps, naming the tools to use"],
) -> str:
    """Save a reusable procedure, or improve an existing one by saving it under the same name.
    Use it when the user asks you to remember how to do something, or after working out a
    multi-step task that will come up again. Asks the user first."""
    try:
        skill = _store(ctx).save(name, description, instructions)
    except SkillError as exc:
        raise ToolError(str(exc)) from exc
    await ctx.runtime.broadcast({"type": "skills.changed"})
    return f"Saved skill '{skill.name}'. It is listed in your instructions from now on."


@tool(category="skills", risk="confirm", summary="Delete skill “{name}”")
async def delete_skill(
    ctx: ToolContext,
    name: Annotated[str, "The skill to delete"],
) -> str:
    """Delete one of the user's or learned skills. Built-in skills can't be deleted. Asks first."""
    store = _store(ctx)
    skill = store.get(name)
    if skill is None:
        raise ToolError(f"There is no skill named '{name}'.")
    if not store.delete(skill.name):
        raise ToolError(f"'{skill.name}' is built in and can't be deleted.")
    await ctx.runtime.broadcast({"type": "skills.changed"})
    return f"Deleted skill '{skill.name}'."
