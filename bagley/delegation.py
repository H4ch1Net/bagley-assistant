"""Sub-agents: hand self-contained subtasks to helper runs that start with an empty context.

Each subtask runs as its own hidden chat (a child of the current one), so the reading and tool
results stay out of the main conversation and only the final answers come back. Nobody watches a
subtask while it runs, so it gets the unattended policy: no tools that need approval, nothing
that changes saved state, and no private reads once it has seen web content.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.runtime import Runtime

MAX_SUBTASKS = 5
PARALLEL = 3  # Local servers queue extra requests anyway; this keeps the queue short.
MAX_ANSWER = 3000

BRIEF = (
    "You are a helper working on one subtask for the main conversation. You start with no "
    "history, so everything you need is below.\n\n{context}Subtask: {task}\n\n"
    "Use your tools as needed, then finish with a concise answer that gives the facts the main "
    "conversation needs, with sources. Don't ask questions; make sensible assumptions and state them."
)


async def run_subtasks(
    rt: Runtime,
    parent_id: str | None,
    tasks: list[str],
    context: str = "",
    parent_policy: Any = None,
) -> list[dict[str, Any]]:
    from bagley.agent import Agent, RunRequest  # Late import: the agent imports the tools.
    from bagley.policy import UnattendedPolicy

    gate = asyncio.Semaphore(PARALLEL)
    background = f"Background: {context.strip()}\n\n" if context.strip() else ""

    async def one(task: str) -> dict[str, Any]:
        conv = rt.store.create_conversation(f"Subtask: {task[:60]}", parent_id=parent_id)
        events: list[dict[str, Any]] = []

        async def emit(event: dict[str, Any]) -> None:
            events.append(event)

        async def deny(call: Any, tool: Any) -> bool:
            return False

        prompt = BRIEF.format(context=background, task=task.strip())
        request = RunRequest(
            text=prompt,
            conversation_id=conv["id"],
            policy=(
                parent_policy.for_helper(prompt)
                if parent_policy is not None
                else UnattendedPolicy.for_prompt(prompt)
            ),
            learn=False,
            exclude=frozenset({"delegate_task", "ask_user"}),
        )
        async with gate:
            await Agent(rt).run(request, emit, deny)
        answers = [
            m["content"]
            for m in rt.store.list_messages(conv["id"])
            if m["role"] == "assistant" and m["content"].strip()
        ]
        errors = [e["message"] for e in events if e["type"] == "error"]
        answer = answers[-1] if answers else ""
        return {
            "task": task,
            "conversation_id": conv["id"],
            "ok": bool(answer) and not errors,
            "answer": answer[:MAX_ANSWER] or (errors[0] if errors else "No answer."),
            "tool_calls": sum(1 for e in events if e["type"] == "tool.end"),
        }

    return list(await asyncio.gather(*(one(t) for t in tasks[:MAX_SUBTASKS])))
