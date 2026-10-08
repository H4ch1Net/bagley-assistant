"""Live readouts for the ctOS bar: CPU, memory, battery, clock and host of this machine."""

from __future__ import annotations

import contextlib
import getpass
from datetime import datetime
from typing import Any

import psutil
from fastapi import APIRouter, Request

from bagley.api import runtime

router = APIRouter()


def _user() -> str:
    with contextlib.suppress(Exception):
        return getpass.getuser()
    return ""


@router.get("/api/system/stats")
async def stats(request: Request) -> dict[str, Any]:
    """Cheap enough to poll every two seconds: CPU is measured since the previous call."""
    rt = runtime(request)
    prefs, _ = rt.preferences()
    vm = psutil.virtual_memory()
    now = datetime.now().astimezone()
    battery = None
    try:
        info = psutil.sensors_battery()
    except (AttributeError, NotImplementedError, OSError):
        info = None
    if info is not None:
        battery = {"percent": round(info.percent), "plugged": bool(info.power_plugged)}
    return {
        "cpu": psutil.cpu_percent(None),
        "mem_used": vm.used,
        "mem_total": vm.total,
        "mem_percent": vm.percent,
        "battery": battery,
        "host": rt.router.machines(prefs)[0].name,
        "user": _user(),
        "tz": now.tzname() or "",
        "time": now.isoformat(timespec="seconds"),
    }
