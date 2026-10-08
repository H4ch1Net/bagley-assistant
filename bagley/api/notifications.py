"""``POST /api/notify/test``: send a test notification and report where it went.

Open windows get it like any notification. The desktop (notify-send) and the phone (ntfy) are
sent directly, so the answer says which of them took it.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from bagley import notify
from bagley.api import runtime
from bagley.runtime import Runtime

router = APIRouter()

Target = Literal["desktop", "phone"]


class NotifyTestBody(BaseModel):
    targets: list[Target] = Field(default_factory=lambda: ["desktop", "phone"], max_length=2)


async def send_test(rt: Runtime, targets: list[str]) -> dict[str, Any]:
    """Send a test to ``targets`` and say, per target, whether it was delivered."""
    prefs, _ = rt.preferences()
    machine = rt.router.machines(prefs)[0].name
    # The lowest level the phone setting lets through, so it looks like a real notification.
    level = notify.LEVELS[notify.NTFY_MIN[prefs.ntfy_level]]
    title = "BAGLEY // LINK TEST"
    body = f"Notification path verified from {machine} at {time.strftime('%H:%M')}."
    windows = await rt.broadcast(
        {"type": "notification", "title": title, "body": body, "level": "info"}
    )
    results: dict[str, dict[str, Any]] = {}
    if "desktop" in targets and not notify.desktop_available():
        results["desktop"] = {"ok": False, "detail": "notify-send is not available here."}
    if "phone" in targets and not prefs.ntfy_url:
        results["phone"] = {"ok": False, "detail": "No ntfy topic set (Settings > Notifications)."}
    send = [t for t in ("desktop", "phone") if t in targets and t not in results]
    if send:
        only = prefs.model_copy(update={"ntfy_url": prefs.ntfy_url if "phone" in send else ""})
        note = notify.Note(title, body, level=level, url=rt.link())
        sent = await notify.fan_out(only, rt.http, note, desktop="desktop" in send)
        done = {"desktop": "Shown.", "phone": f"Sent to ntfy as {level}."}
        failed = {
            "desktop": "notify-send failed.",
            "phone": "ntfy did not take it (check the URL and token).",
        }
        for target in send:
            ok = bool(sent.get(target))
            results[target] = {"ok": ok, "detail": done[target] if ok else failed[target]}
    results = {t: results[t] for t in ("desktop", "phone") if t in results}
    return {"level": level, "windows": windows, "results": results}


@router.post("/api/notify/test")
async def notify_test(body: NotifyTestBody, request: Request) -> dict[str, Any]:
    return await send_test(runtime(request), list(dict.fromkeys(body.targets)))
