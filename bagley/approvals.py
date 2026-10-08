"""Approvals shared by every client.

A tool that needs approval waits here until someone answers: the chat window that started the
run, any other open window, the desktop overlay, a mako notification action or the phone. The
first answer wins. Nobody answering within the timeout counts as a denial.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.runtime import Runtime

DECISIONS = ("allow", "deny", "always")
TIMEOUT = 600.0


@dataclass
class Pending:
    id: str
    tool: str
    arguments: dict[str, Any]
    summary: str
    conversation_id: str | None
    source: str
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future[str] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": self.arguments,
            "summary": self.summary,
            "conversation_id": self.conversation_id,
            "source": self.source,
            "created_at": self.created_at,
        }


def summarize(template: str, name: str, args: dict[str, Any]) -> str:
    """Fill a tool's summary template ("Run `{command}`") from its arguments."""
    if not template:
        return name.replace("_", " ").capitalize()

    class Missing(dict):
        def __missing__(self, key: str) -> str:
            return "…"

    def short(value: Any) -> str:
        text = value if isinstance(value, str) else str(value)
        return text if len(text) <= 80 else text[:77] + "…"

    try:
        return template.format_map(Missing({k: short(v) for k, v in args.items()}))
    except (ValueError, IndexError):
        return template


class ApprovalBroker:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self.items: dict[str, Pending] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def pending(self) -> list[dict[str, Any]]:
        return [p.public() for p in self.items.values()]

    async def request(self, item: Pending, *, timeout: float = TIMEOUT) -> str:
        """Wait for a decision on ``item``. Returns "allow", "always" or "deny"."""
        loop = asyncio.get_running_loop()
        item.future = loop.create_future()
        self.items[item.id] = item
        await self.rt.broadcast({"type": "approval.pending", **item.public()})
        self._offer_on_desktop(item)
        try:
            decision = await asyncio.wait_for(asyncio.shield(item.future), timeout=timeout)
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            self.items.pop(item.id, None)
        await self.rt.broadcast({"type": "approval.resolved", "id": item.id, "decision": decision})
        return decision

    def resolve(self, approval_id: str, decision: str) -> bool:
        item = self.items.get(approval_id)
        if not item or not item.future or item.future.done():
            return False
        item.future.set_result(decision if decision in DECISIONS else "deny")
        return True

    def deny_all(self, ids: set[str] | None = None) -> None:
        for key in list(self.items):
            if ids is None or key in ids:
                self.resolve(key, "deny")

    def _offer_on_desktop(self, item: Pending) -> None:
        """Ask through a desktop notification with Allow and Deny actions as well, when the
        request did not come from a browser window (which shows its own prompt)."""
        if item.source == "web":
            return
        prefs, _ = self.rt.preferences()
        if not prefs.desktop_notifications:
            return
        from bagley import notify

        if not notify.desktop_available():
            return

        async def ask() -> None:
            note = notify.Note(
                f"BAGLEY // APPROVAL: {item.tool}",
                item.summary,
                level="critical",
                actions=[("allow", "Allow"), ("deny", "Deny")],
            )
            choice = await notify.send_desktop(note, wait=TIMEOUT)
            if choice in ("allow", "deny"):
                self.resolve(item.id, choice)

        task = asyncio.create_task(ask())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        self.deny_all()
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
