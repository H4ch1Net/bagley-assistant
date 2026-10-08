"""Tools that reach the computer Bagley runs on: system status, opening things, notifications."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import psutil

from bagley.tools import ToolContext, ToolError, tool
from bagley.tools.files import resolve


def _gb(n: float) -> float:
    return round(n / 1024**3, 1)


@tool(category="system", summary="Check this computer's status")
async def system_status(
    top: Annotated[int, "How many of the busiest processes to list (0-15)"] = 5,
) -> dict[str, Any]:
    """Live load on the user's computer right now: CPU and memory in use, free disk, battery,
    uptime and the busiest processes. Use it for "why is my laptop slow?". For what hardware
    the computer has, use system_specs."""
    procs = list(psutil.process_iter(["pid", "name", "memory_info"]))
    for p in procs:  # First sample; cpu_percent needs two readings.
        with contextlib.suppress(psutil.Error, OSError):
            p.cpu_percent(None)
    cpu = await asyncio.to_thread(psutil.cpu_percent, 0.5)
    rows = []
    for p in procs:
        try:
            mem = p.info["memory_info"].rss if p.info.get("memory_info") else 0
            rows.append(
                {
                    "pid": p.pid,
                    "name": p.info.get("name") or "?",
                    "cpu": p.cpu_percent(None),
                    "mem": mem,
                }
            )
        except (psutil.Error, OSError):
            continue
    top = max(0, min(top, 15))
    vm = psutil.virtual_memory()
    disk = shutil.disk_usage(Path.home())
    status: dict[str, Any] = {
        "cpu_percent": cpu,
        "cpu_cores": psutil.cpu_count(),
        "memory": {"used_gb": _gb(vm.used), "total_gb": _gb(vm.total), "percent": vm.percent},
        "disk_home": {"free_gb": _gb(disk.free), "total_gb": _gb(disk.total)},
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "top_cpu": [
            {"name": r["name"], "pid": r["pid"], "cpu_percent": round(r["cpu"], 1)}
            for r in sorted(rows, key=lambda r: r["cpu"], reverse=True)[:top]
        ],
        "top_memory": [
            {"name": r["name"], "pid": r["pid"], "memory_mb": round(r["mem"] / 1024**2)}
            for r in sorted(rows, key=lambda r: r["mem"], reverse=True)[:top]
        ],
    }
    if hasattr(os, "getloadavg"):
        status["load_average"] = [round(x, 2) for x in os.getloadavg()]
    try:
        battery = psutil.sensors_battery()
    except (AttributeError, NotImplementedError, OSError):
        battery = None
    if battery is not None:
        status["battery"] = {"percent": round(battery.percent), "plugged_in": battery.power_plugged}
    return status


@tool(category="system", summary="Read this computer's specs")
async def system_specs() -> dict[str, Any]:
    """What the user's computer is: machine model, CPU, GPU, RAM, storage, OS and kernel,
    displays and battery health. Use it for "what are my specs?", "what GPU do I have?",
    "can I run this?". For what is using the CPU or memory right now, use system_status.
    `summary` is a ready spec sheet: show it in a code block and add a line of your own."""
    from bagley import specs

    return await asyncio.to_thread(specs.collect)


def _open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if not shutil.which(opener):
            raise ToolError("No desktop is available to open files on this computer.")
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@tool(category="system", risk="confirm", summary="Open {target}")
def open_on_computer(
    ctx: ToolContext,
    target: Annotated[str, "An http(s) URL, or a file or folder path relative to the workspace"],
) -> str:
    """Open a web page in the user's browser, or a workspace file or folder in its default app,
    on the user's own screen. Asks the user first."""
    parts = urlsplit(target.strip())
    if parts.scheme in ("http", "https") and parts.hostname:
        if not webbrowser.open(target.strip()):
            raise ToolError("No browser is available on this computer.")
        return f"Opened {target} in the browser."
    if parts.scheme and len(parts.scheme) > 1:
        raise ToolError("Only http(s) URLs and workspace paths can be opened.")
    path = resolve(ctx, target)
    if not path.exists():
        raise ToolError(f"'{target}' does not exist.")
    _open_path(path)
    return f"Opened {target}."


@tool(category="system", summary="Notify: {title}")
async def notify_user(
    ctx: ToolContext,
    title: Annotated[str, "Short headline"],
    message: Annotated[str, "One or two sentences"] = "",
) -> str:
    """Send the user a notification (toast plus a desktop notification if they allowed them).
    Useful in automations, e.g. only notify when a condition is met."""
    runtime = ctx.runtime
    if runtime is None:
        raise ToolError("Notifications are not available here.")
    delivered = await runtime.notify(
        title[:120], message[:500], conversation_id=ctx.conversation_id
    )
    return (
        "Notification shown."
        if delivered
        else "No window is open; the chat is marked unread instead."
    )
