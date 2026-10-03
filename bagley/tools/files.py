"""File tools, sandboxed to the workspace directory (``BAGLEY_WORKSPACE``)."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Annotated, Any

from bagley.tools import ToolContext, ToolError, tool

MAX_READ_CHARS = 20_000
MAX_WRITE_CHARS = 200_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


def resolve(ctx: ToolContext, path: str) -> Path:
    """Resolve ``path`` inside the workspace. Rejects anything that escapes it, symlinks included."""
    root = Path(ctx.config.workspace or ".").resolve()
    cleaned = (path or ".").strip().replace("\\", "/")
    target = (root / cleaned.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"'{path}' is outside the workspace. Use paths relative to the workspace.")
    return target


def _rel(ctx: ToolContext, path: Path) -> str:
    root = Path(ctx.config.workspace or ".").resolve()
    return path.relative_to(root).as_posix() or "."


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


@tool(category="files", summary="List files in {path}")
def list_files(
    ctx: ToolContext,
    path: Annotated[str, "Directory relative to the workspace"] = ".",
    pattern: Annotated[str, "Optional glob filter such as '*.md'"] = "",
) -> dict[str, Any]:
    """List files and folders in the user's workspace directory."""
    target = resolve(ctx, path)
    if not target.exists():
        raise ToolError(f"'{path}' does not exist.")
    if not target.is_dir():
        raise ToolError(f"'{path}' is a file, not a directory.")
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if pattern and not fnmatch.fnmatch(child.name, pattern):
            continue
        if child.is_dir():
            entries.append({"name": child.name + "/", "type": "dir"})
        else:
            entries.append(
                {"name": child.name, "type": "file", "size": _human(child.stat().st_size)}
            )
        if len(entries) >= 200:
            break
    return {"path": _rel(ctx, target), "entries": entries, "workspace": str(ctx.config.workspace)}


@tool(category="files", summary="Read {path}")
def read_file(
    ctx: ToolContext,
    path: Annotated[str, "File path relative to the workspace"],
    offset: Annotated[int, "Character offset to start reading from"] = 0,
) -> dict[str, Any]:
    """Read a text file from the workspace."""
    target = resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"'{path}' is not a file.")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"'{path}' is not a UTF-8 text file.") from exc
    chunk = text[max(0, offset) : max(0, offset) + MAX_READ_CHARS]
    end = max(0, offset) + len(chunk)
    return {
        "path": _rel(ctx, target),
        "content": chunk,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
    }


@tool(category="files", summary="Search files for “{query}”")
def search_files(
    ctx: ToolContext,
    query: Annotated[str, "Text to look for (case-insensitive)"],
    path: Annotated[str, "Directory to search, relative to the workspace"] = ".",
) -> dict[str, Any]:
    """Find lines containing some text across files in the workspace."""
    target = resolve(ctx, path)
    needle = query.lower()
    matches: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            file = Path(dirpath) / name
            try:
                if file.stat().st_size > 2_000_000:
                    continue
                with file.open(encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if needle in line.lower():
                            matches.append(
                                {
                                    "file": _rel(ctx, file),
                                    "line": lineno,
                                    "text": line.strip()[:200],
                                }
                            )
                            if len(matches) >= 50:
                                return {"matches": matches, "truncated": True}
            except (UnicodeDecodeError, OSError):
                continue
    return {"matches": matches, "truncated": False}


@tool(category="files", risk="confirm", summary="Write {path}")
def write_file(
    ctx: ToolContext,
    path: Annotated[str, "File path relative to the workspace"],
    content: Annotated[str, "Full text content to write"],
    append: Annotated[bool, "Append instead of overwriting"] = False,
) -> str:
    """Create or overwrite a text file in the workspace (or append to it). Asks the user first."""
    if len(content) > MAX_WRITE_CHARS:
        raise ToolError("Content is too large.")
    target = resolve(ctx, path)
    if target.is_dir():
        raise ToolError(f"'{path}' is a directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    with target.open("a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)
    verb = "Appended to" if append else ("Overwrote" if existed else "Created")
    return f"{verb} {_rel(ctx, target)} ({len(content)} chars)."
