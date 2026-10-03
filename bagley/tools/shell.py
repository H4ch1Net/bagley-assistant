"""Shell access. Only registered when ``BAGLEY_ENABLE_SHELL=true``; every run asks the user."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from typing import Annotated, Any

from bagley.tools import ToolContext, ToolError, tool

MAX_OUTPUT = 8000


@tool(category="system", risk="confirm", summary="Run `{command}`", timeout=130)
async def run_command(
    ctx: ToolContext,
    command: Annotated[str, "Shell command to run in the workspace directory"],
    timeout: Annotated[int, "Seconds before the command is stopped (max 120)"] = 60,
) -> dict[str, Any]:
    """Run a shell command in the workspace and return its output. The user approves each run."""
    shell = os.environ.get("COMSPEC", "cmd.exe") if sys.platform == "win32" else "/bin/sh"
    flag = "/c" if sys.platform == "win32" else "-c"
    # Run in its own process group so a timeout or stop also ends anything it started.
    group = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_exec(
        shell,
        flag,
        command,
        cwd=str(ctx.config.workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        **group,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=max(1, min(timeout, 120)))
    except asyncio.TimeoutError as exc:
        _kill_tree(proc)
        raise ToolError(f"Command timed out after {timeout}s and was stopped.") from exc
    except asyncio.CancelledError:
        _kill_tree(proc)
        raise
    text = out.decode("utf-8", "replace")
    if len(text) > MAX_OUTPUT:
        text = text[: MAX_OUTPUT // 2] + "\n…[output truncated]…\n" + text[-MAX_OUTPUT // 2 :]
    return {"exit_code": proc.returncode, "output": text}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
