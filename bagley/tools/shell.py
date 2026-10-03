"""Shell and Python access. Only registered when ``BAGLEY_ENABLE_SHELL=true``; every run asks
the user first."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from bagley.tools import ToolContext, ToolError, ToolOutput, tool

MAX_OUTPUT = 8000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGES = 6
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "site-packages"}

# Runs the snippet with a clean traceback, then saves any matplotlib figures still open.
RUNNER = r"""
import linecache, os, sys, traceback, warnings
src, out = sys.argv[1], sys.argv[2]
warnings.filterwarnings("ignore", message=".*non-interactive.*")
with open(src, encoding="utf-8") as f:
    text = f.read()
linecache.cache["<snippet>"] = (len(text), None, text.splitlines(True), "<snippet>")
sys.argv = ["<snippet>"]
sys.path.insert(0, os.getcwd())
status = 0
try:
    exec(compile(text, "<snippet>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
except SystemExit as exc:
    status = exc.code
except BaseException as exc:
    traceback.print_exception(type(exc), exc, exc.__traceback__.tb_next)
    status = 1
plt = sys.modules.get("matplotlib.pyplot")
for i, num in enumerate(plt.get_fignums() if plt else [], 1):
    try:
        plt.figure(num).savefig(os.path.join(out, f"figure-{i}.png"), dpi=110, bbox_inches="tight")
    except Exception as exc:
        print(f"Could not save figure {i}: {exc}", file=sys.stderr)
sys.exit(status)
"""


async def _run(
    args: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> tuple[int | None, str]:
    # Run in its own process group so a timeout or stop also ends anything it started.
    group = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        env=env,
        **group,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        _kill_tree(proc)
        raise ToolError(f"It timed out after {timeout}s and was stopped.") from exc
    except asyncio.CancelledError:
        _kill_tree(proc)
        raise
    text = out.decode("utf-8", "replace")
    if len(text) > MAX_OUTPUT:
        text = text[: MAX_OUTPUT // 2] + "\n…[output truncated]…\n" + text[-MAX_OUTPUT // 2 :]
    return proc.returncode, text


@tool(category="system", risk="confirm", summary="Run `{command}`", timeout=130)
async def run_command(
    ctx: ToolContext,
    command: Annotated[str, "Shell command to run in the workspace directory"],
    timeout: Annotated[int, "Seconds before the command is stopped (max 120)"] = 60,
) -> dict[str, Any]:
    """Run a shell command in the workspace and return its output. The user approves each run."""
    shell = os.environ.get("COMSPEC", "cmd.exe") if sys.platform == "win32" else "/bin/sh"
    flag = "/c" if sys.platform == "win32" else "-c"
    code, text = await _run(
        [shell, flag, command], Path(ctx.config.workspace), max(1, min(timeout, 120))
    )
    return {"exit_code": code, "output": text}


def _new_images(root: Path, since: float) -> list[Path]:
    """Image files written under the workspace since ``since`` (a few levels deep)."""
    found: list[tuple[float, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = (
            []
            if depth >= 3
            else [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
        )
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in IMAGE_SUFFIXES:
                with contextlib.suppress(OSError):
                    if (mtime := path.stat().st_mtime) >= since:
                        found.append((mtime, path))
    return [path for _, path in sorted(found)[:MAX_IMAGES]]


@tool(category="system", risk="confirm", summary="Run Python code", timeout=310)
async def run_python(
    ctx: ToolContext,
    code: Annotated[str, "A complete Python script. Print what you need to see."],
    timeout: Annotated[int, "Seconds before it is stopped (max 300)"] = 60,
) -> ToolOutput:
    """Run a Python script in the workspace folder and return its output. Use it for maths,
    data analysis (CSV, Excel, JSON in the workspace) and charts. Matplotlib figures left open
    are saved to charts/ and shown to the user, so there is no need to call savefig or show.
    The user approves each run."""
    root = Path(ctx.config.workspace).resolve()
    python = os.path.expanduser(ctx.config.python) if ctx.config.python else sys.executable
    env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    started = time.time() - 1
    with tempfile.TemporaryDirectory(prefix="bagley-run-") as tmp:
        runner, src, out = (Path(tmp) / n for n in ("runner.py", "snippet.py", "figures"))
        out.mkdir()
        runner.write_text(RUNNER, encoding="utf-8")
        src.write_text(code, encoding="utf-8")
        try:
            exit_code, text = await _run(
                [python, str(runner), str(src), str(out)], root, max(1, min(timeout, 300)), env
            )
        except FileNotFoundError as exc:
            raise ToolError(f"Python interpreter not found: {python}") from exc
        images = _new_images(root, started)
        figures = sorted(out.glob("figure-*.png"))
        if figures and not images:  # The script didn't save anything itself; keep its figures.
            charts = root / "charts"
            charts.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for fig in figures[:MAX_IMAGES]:
                dest = charts / f"{stamp}-{fig.name}"
                shutil.move(str(fig), dest)
                images.append(dest)
    rel = [p.relative_to(root).as_posix() for p in images]
    result: dict[str, Any] = {"exit_code": exit_code, "output": text or "(no output)"}
    if rel:
        result["images"] = [f"{r} (shown to the user)" for r in rel]
    ui = {"images": [{"path": r, "url": f"/api/workspace/raw?path={quote(r)}"} for r in rel]}
    return ToolOutput(result, ui if rel else {})


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
