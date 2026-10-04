"""Tools for working through bigger tasks: a visible plan, questions to the user, and recall of
earlier conversations."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from bagley.tools import ToolContext, ToolError, ToolOutput, tool

MARKS = {"x": "done", "✓": "done", ">": "doing", "~": "doing", " ": "todo", "": "todo"}


def _step(item: Any) -> dict[str, str]:
    if isinstance(item, dict):
        text = str(item.get("step") or item.get("text") or item.get("title") or "")
        status = str(item.get("status", "todo")).lower()
        status = {"completed": "done", "in_progress": "doing", "pending": "todo"}.get(
            status, status
        )
        return {"text": text.strip(), "status": status if status in MARKS.values() else "todo"}
    m = re.match(r"^\s*(?:[-*]\s*)?\[([ xX✓>~]?)\]\s*(.*)$", str(item))
    if m:
        return {"text": m.group(2).strip(), "status": MARKS.get(m.group(1).lower(), "todo")}
    return {"text": str(item).strip(), "status": "todo"}


@tool(category="agent", summary="Update the plan")
def update_plan(
    steps: Annotated[
        list[str],
        "Every step in order. Start each with [x] when done, [>] for the one you're on, "
        "or [ ] when still to do",
    ],
) -> ToolOutput:
    """Show the user a short checklist for a task that takes several steps. Call it once with
    the plan before you start, then again whenever a step is finished."""
    plan = [s for s in (_step(i) for i in steps[:20]) if s["text"]]
    if not plan:
        raise ToolError("The plan needs at least one step.")
    done = sum(s["status"] == "done" for s in plan)
    nxt = next((s["text"] for s in plan if s["status"] != "done"), None)
    text = f"Plan updated: {done} of {len(plan)} done."
    if nxt:
        text += f" Next: {nxt}"
    return ToolOutput(text, ui={"plan": plan})


@tool(category="agent", summary="Ask: {question}", timeout=900)
async def ask_user(
    ctx: ToolContext,
    question: Annotated[str, "One short question"],
    options: Annotated[list[str], "Two to four likely answers, if there are obvious ones"] = [],  # noqa: B006
) -> str:
    """Ask the user to decide something or fill in a missing detail before you continue, instead
    of guessing. Don't use it for things you can find out with other tools."""
    if not question.strip():
        raise ToolError("The question is empty.")
    unavailable = (
        "Nobody can answer right now. Make a sensible assumption, carry on, and say in your "
        "answer what you assumed."
    )
    if ctx.ask is None:
        return unavailable
    answer = await ctx.ask(question.strip()[:300], [str(o)[:80] for o in options[:6] if str(o)])
    if answer is None or not str(answer).strip():
        return unavailable
    return f"The user answered: {str(answer).strip()[:1000]}"


@tool(category="agent", summary="Search past chats for “{query}”")
def search_chats(
    ctx: ToolContext,
    query: Annotated[str, "Words to look for, e.g. 'flight Tokyo'"] = "",
    conversation_id: Annotated[str, "Read this conversation instead of searching"] = "",
) -> dict[str, Any] | str:
    """Search earlier conversations with the user, e.g. "what did we decide about the trip?" or
    "the recipe from last week". Pass conversation_id from a result to read that chat."""
    store = ctx.store
    if conversation_id:
        conv = store.get_conversation(conversation_id.strip())
        if not conv:
            raise ToolError("No conversation with that id.")
        lines, size = [], 0
        for m in store.list_messages(conv["id"]):
            if m["role"] not in ("user", "assistant") or not m["content"].strip():
                continue
            line = f"{m['role']}: {m['content'].strip()}"
            lines.append(line[:1500])
            size += len(lines[-1])
            if size > 6000:
                lines.append("…")
                break
        return {"title": conv["title"], "messages": lines}
    if not query.strip():
        raise ToolError("Give a query or a conversation_id.")
    hits = store.search_messages(query, exclude=ctx.conversation_id)
    if not hits:
        return f"No earlier messages mention '{query}'."
    words = [w for w in re.findall(r"\w{2,}", query.lower())]
    results = []
    for h in hits:
        text = " ".join(h["content"].split())
        pos = min((text.lower().find(w) for w in words if w in text.lower()), default=0)
        start = max(0, pos - 120)
        snippet = ("…" if start else "") + text[start : start + 320]
        results.append(
            {
                "conversation_id": h["conversation_id"],
                "title": h["title"],
                "date": datetime.fromtimestamp(h["created_at"]).strftime("%Y-%m-%d"),
                "from": h["role"],
                "text": snippet + ("…" if start + 320 < len(text) else ""),
            }
        )
    return {"results": results}


@tool(category="agent", summary="Delegate: {tasks}", timeout=1800)
async def delegate_task(
    ctx: ToolContext,
    tasks: Annotated[
        list[str],
        "One to five self-contained subtasks. Each must include every detail it needs, "
        "because the helper sees nothing of this conversation",
    ],
    context: Annotated[str, "Background that every subtask needs (optional)"] = "",
) -> ToolOutput:
    """Hand subtasks to helper agents that each start with a fresh, empty context and their own
    tools, and get only their final answers back. Use it for research across several sources or
    items, or any long reading that would crowd this conversation. Helpers can't ask the user
    anything or use tools that need approval."""
    from bagley.delegation import MAX_SUBTASKS, run_subtasks

    if ctx.runtime is None:
        raise ToolError("Delegation is not available here.")
    todo = [t.strip() for t in tasks if str(t).strip()]
    if not todo:
        raise ToolError("Give at least one subtask.")
    if len(todo) > MAX_SUBTASKS:
        raise ToolError(f"At most {MAX_SUBTASKS} subtasks at a time.")
    results = await run_subtasks(ctx.runtime, ctx.conversation_id, todo, context)
    return ToolOutput(
        {"results": [{"task": r["task"], "answer": r["answer"]} for r in results]},
        ui={
            "subtasks": [
                {
                    "task": r["task"],
                    "conversation_id": r["conversation_id"],
                    "ok": r["ok"],
                    "tool_calls": r["tool_calls"],
                }
                for r in results
            ]
        },
    )
