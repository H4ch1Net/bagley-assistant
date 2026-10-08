"""Shell and Python access. Only registered when ``BAGLEY_ENABLE_SHELL=true``; every run asks
the user first.

With the sandbox on (``Preferences.sandbox == "bwrap"``) both tools run inside bubblewrap: the
system is read-only, the home folder, /run and Bagley's data are hidden, only the workspace and
a private /tmp can be written, there is no network unless ``sandbox_network`` is on, and the
environment is cleared. When ``bwrap`` is missing the tools refuse to run rather than run
unsandboxed.
"""

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
from collections.abc import Iterable
from pathlib import Path, PurePath, PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

from bagley.config import resolve_preferences
from bagley.tools import ToolContext, ToolError, ToolOutput, tool


def available(config: Any) -> bool:
    return bool(config.enable_shell)


MAX_OUTPUT = 8000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGES = 6
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "site-packages"}

# Inside the sandbox: a fixed PATH and a throwaway home on the private /tmp.
SANDBOX_HOME = "/tmp/home"
SANDBOX_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "HOME": SANDBOX_HOME,
    "TERM": "dumb",
    "MPLBACKEND": "Agg",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}
NO_BWRAP = (
    "Not run: the sandbox is on but bubblewrap (bwrap) is not installed. Install it "
    "(`sudo apt install bubblewrap`) or switch the sandbox off in Settings."
)

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


# Sandbox ----------------------------------------------------------------------------------------


def _within(path: PurePath, parent: PurePath) -> bool:
    return path == parent or parent in path.parents


def bwrap_args(
    workspace: Path | str,
    *,
    network: bool,
    extra_ro: Iterable[Path | str] = (),
    extra_rw: Iterable[Path | str] = (),
    hide: Iterable[Path | str] = (),
    home: Path | str | None = None,
) -> list[str]:
    """Arguments for ``bwrap``, without the program and the command to run.

    The whole system is mounted read-only with fresh /dev, /proc and /tmp. The home folder and
    every ``hide`` path are covered with empty tmpfs mounts, then the workspace is bound back
    writable, so it stays usable when it lives under the home folder. ``extra_ro`` and
    ``extra_rw`` are bound last (an interpreter, a temp folder). Every namespace is unshared
    (the network too unless ``network``), the environment is cleared and all capabilities are
    dropped. Paths should be absolute and resolved. They are Linux paths whatever runs this.
    """
    ws = PurePosixPath(workspace)
    home_dir = PurePosixPath(home if home is not None else Path.home())
    args = ["--die-with-parent", "--new-session", "--unshare-all"]
    if network:
        args.append("--share-net")
    args += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    args += ["--tmpfs", "/tmp", "--dir", SANDBOX_HOME, "--tmpfs", str(home_dir)]
    hidden = [PurePosixPath(p) for p in hide]
    inside = [p for p in hidden if p != ws and _within(p, ws)]
    outside = [p for p in hidden if p != ws and p not in inside]
    for path in outside:
        # Skip what the home folder or another hidden folder covers already.
        if any(_within(path, cover) for cover in (home_dir, *outside) if cover != path):
            continue
        args += ["--tmpfs", str(path)]
    args += ["--bind", str(ws), str(ws)]
    for path in inside:  # Bagley's data inside the workspace stays hidden too.
        args += ["--tmpfs", str(path)]
    for path in extra_ro:
        args += ["--ro-bind", str(path), str(path)]
    for path in extra_rw:
        args += ["--bind", str(path), str(path)]
    args += ["--chdir", str(ws), "--clearenv"]
    for key, value in SANDBOX_ENV.items():
        args += ["--setenv", key, value]
    return [*args, "--cap-drop", "ALL"]


def sandbox_settings(ctx: ToolContext) -> tuple[bool, bool]:
    """Whether the sandbox is on, and whether it may use the network."""
    if ctx.runtime is not None:
        prefs, _ = ctx.runtime.preferences()
    else:
        prefs, _ = resolve_preferences(ctx.store.get_preferences(), {})
    return prefs.sandbox == "bwrap", prefs.sandbox_network


def _hidden_paths(ctx: ToolContext) -> list[Path]:
    """Folders a sandboxed command must not see besides the home folder: Bagley's data (the
    database, captures, plugins) and the runtime folders. A read-only mount still lets a
    process connect to the sockets in /run: the D-Bus session bus could ask the desktop to
    start programs outside the sandbox, and the Docker socket is root on the host."""
    paths = [Path(ctx.config.data_dir), Path("/run")]
    if os.environ.get("XDG_RUNTIME_DIR"):
        paths.append(Path(os.environ["XDG_RUNTIME_DIR"]))
    return [p.resolve() for p in paths if p.is_dir()]


def _resolver(hidden: list[Path], conf: Path = Path("/etc/resolv.conf")) -> list[Path]:
    """/etc/resolv.conf often points into /run (systemd-resolved, NetworkManager); with the
    network on, bind that one file back so names still resolve."""
    target = conf.resolve()
    if target.is_file() and any(_within(target, h) for h in hidden):
        return [target]
    return []


def _python_dirs(python: str, home: Path, hidden: list[Path]) -> list[Path]:
    """Folders under the home folder that an interpreter needs (a virtualenv, pyenv, conda)."""
    exe = Path(shutil.which(python) or python).absolute()
    found: set[Path] = set()
    for path in (exe, exe.resolve()):
        root = path.parent.parent
        if _within(home, root) or any(_within(h, root) for h in hidden):
            root = path.parent  # Never bind the home folder or Bagley's data back in.
        if root != home and _within(root, home):
            found.add(root)
    return sorted(found)


def _sandboxed(
    ctx: ToolContext,
    args: list[str],
    *,
    network: bool,
    python: str = "",
    writable: Iterable[Path] = (),
) -> list[str]:
    """``args`` wrapped in bwrap. Refuses when bwrap is missing: never runs unsandboxed."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise ToolError(NO_BWRAP)
    home = Path.home().resolve()
    hidden = _hidden_paths(ctx)
    readable = _python_dirs(python, home, hidden) if python else []
    if network:
        readable += _resolver(hidden)
    sandbox = bwrap_args(
        Path(ctx.config.workspace).resolve(),
        network=network,
        extra_ro=readable,
        extra_rw=writable,
        hide=hidden,
        home=home,
    )
    return [bwrap, *sandbox, *args]


def _check_started(code: int | None, text: str) -> None:
    """bwrap reports its own setup failures as ``bwrap: ...`` with exit code 1."""
    if code and text.startswith("bwrap: "):
        reason = text.splitlines()[0].removeprefix("bwrap: ")
        raise ToolError(
            f"The sandbox could not start ({reason}). Unprivileged user namespaces may be "
            "disabled on this system: allow them, or switch the sandbox off in Settings."
        )


def _label(network: bool) -> str:
    return "bwrap" if network else "bwrap, no network"


# Tools ------------------------------------------------------------------------------------------


@tool(category="system", risk="confirm", summary="Run `{command}`", timeout=130)
async def run_command(
    ctx: ToolContext,
    command: Annotated[str, "Shell command to run in the workspace directory"],
    timeout: Annotated[int, "Seconds before the command is stopped (max 120)"] = 60,
) -> ToolOutput:
    """Run a shell command in the workspace and return its output. The user approves each run."""
    sandboxed, network = sandbox_settings(ctx)
    if sandboxed:
        args = _sandboxed(ctx, ["/bin/sh", "-c", command], network=network)
    elif sys.platform == "win32":
        args = [os.environ.get("COMSPEC", "cmd.exe"), "/c", command]
    else:
        args = ["/bin/sh", "-c", command]
    code, text = await _run(args, Path(ctx.config.workspace), max(1, min(timeout, 120)))
    if not sandboxed:
        return ToolOutput({"exit_code": code, "output": text})
    _check_started(code, text)
    return ToolOutput(
        {"exit_code": code, "output": text, "sandbox": _label(network)}, {"sandboxed": True}
    )


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
    sandboxed, network = sandbox_settings(ctx)
    env = {**os.environ, "MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    started = time.time() - 1
    with tempfile.TemporaryDirectory(prefix="bagley-run-") as tmp:
        folder = Path(tmp).resolve()
        runner, src, out = (folder / n for n in ("runner.py", "snippet.py", "figures"))
        out.mkdir()
        runner.write_text(RUNNER, encoding="utf-8")
        src.write_text(code, encoding="utf-8")
        args = [python, str(runner), str(src), str(out)]
        if sandboxed:  # The runner folder is bound in writable, for the figures.
            args = _sandboxed(ctx, args, network=network, python=python, writable=[folder])
        try:
            exit_code, text = await _run(args, root, max(1, min(timeout, 300)), env)
        except FileNotFoundError as exc:
            raise ToolError(f"Python interpreter not found: {python}") from exc
        if sandboxed:
            _check_started(exit_code, text)
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
    ui: dict[str, Any] = {}
    if rel:
        result["images"] = [f"{r} (shown to the user)" for r in rel]
        ui["images"] = [{"path": r, "url": f"/api/workspace/raw?path={quote(r)}"} for r in rel]
    if sandboxed:
        result["sandbox"] = _label(network)
        ui["sandboxed"] = True
    return ToolOutput(result, ui)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
