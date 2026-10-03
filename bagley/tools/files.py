"""File tools, sandboxed to the workspace directory (``BAGLEY_WORKSPACE``)."""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import Annotated, Any

from bagley.journal import file_hash, snapshot, text_diff
from bagley.tools import ToolContext, ToolError, ToolOutput, tool

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


def resolve_entry(ctx: ToolContext, path: str) -> Path:
    """Like ``resolve``, but a symlink stays the link itself, so moving or deleting it acts on
    the link and not on the file it points to."""
    target = resolve(ctx, path)
    root = Path(ctx.config.workspace or ".").resolve()
    link = root / (path or ".").strip().replace("\\", "/").lstrip("/")
    if link.is_symlink():
        link = link.parent.resolve() / link.name
        if link.parent == root or root in link.parent.parents:
            return link
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
    root = Path(ctx.config.workspace or ".").resolve()
    needle = query.lower()
    matches: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            file = Path(dirpath) / name
            real = file.resolve()
            if real != root and root not in real.parents:  # Symlink pointing outside.
                continue
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


def _read_text(target: Path) -> str:
    if not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def _changed(
    ctx: ToolContext, action: str, target: Path, old: str, new: str, backup: str | None, verb: str
) -> ToolOutput:
    rel = _rel(ctx, target)
    jid = ctx.store.add_journal(
        conversation_id=ctx.conversation_id,
        action=action,
        path=rel,
        backup=backup,
        after_hash=file_hash(target),
    )
    return ToolOutput(
        f"{verb} {rel} ({len(new)} chars).",
        ui={"journal_id": jid, "path": rel, "diff": text_diff(old, new, rel)},
    )


@tool(category="files", risk="confirm", summary="Write {path}")
def write_file(
    ctx: ToolContext,
    path: Annotated[str, "File path relative to the workspace"],
    content: Annotated[str, "Full text content to write"],
    append: Annotated[bool, "Append instead of overwriting"] = False,
) -> ToolOutput:
    """Create or overwrite a text file in the workspace (or append to it). Asks the user first.
    Every change can be reverted from the chat."""
    if len(content) > MAX_WRITE_CHARS:
        raise ToolError("Content is too large.")
    target = resolve(ctx, path)
    if target.is_dir():
        raise ToolError(f"'{path}' is a directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    old = _read_text(target)
    backup = snapshot(ctx.config, target)
    with target.open("a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)
    verb = "Appended to" if append else ("Overwrote" if existed else "Created")
    return _changed(ctx, "write", target, old, old + content if append else content, backup, verb)


@tool(category="files", risk="confirm", summary="Edit {path}")
def edit_file(
    ctx: ToolContext,
    path: Annotated[str, "File path relative to the workspace"],
    find: Annotated[str, "Exact text to replace (include enough context to be unique)"],
    replace: Annotated[str, "Replacement text"],
    all_occurrences: Annotated[bool, "Replace every occurrence instead of exactly one"] = False,
) -> ToolOutput:
    """Replace exact text in a workspace file without rewriting all of it. Asks the user first."""
    target = resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"'{path}' is not a file.")
    try:  # newline="" keeps the file's own line endings.
        with target.open(encoding="utf-8", newline="") as f:
            old = f.read()
    except UnicodeDecodeError as exc:
        raise ToolError(f"'{path}' is not a UTF-8 text file.") from exc
    if "\r\n" in old and "\r\n" not in find:
        find, replace = find.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
    count = old.count(find) if find else 0
    if count == 0:
        raise ToolError("The text to replace was not found. Read the file and copy it exactly.")
    if count > 1 and not all_occurrences:
        raise ToolError(f"The text appears {count} times. Add more context or set all_occurrences.")
    new = old.replace(find, replace) if all_occurrences else old.replace(find, replace, 1)
    backup = snapshot(ctx.config, target)
    with target.open("w", encoding="utf-8", newline="") as f:
        f.write(new)
    return _changed(
        ctx, "edit", target, old, new, backup, f"Edited ({count} replacement{'s' * (count > 1)})"
    )


@tool(category="files", risk="confirm", summary="Move {source} to {destination}")
def move_file(
    ctx: ToolContext,
    source: Annotated[str, "File or folder to move, relative to the workspace"],
    destination: Annotated[str, "New path, relative to the workspace"],
) -> ToolOutput:
    """Move or rename a file or folder in the workspace. Asks the user first; can be reverted."""
    src = resolve_entry(ctx, source)
    dst = resolve(ctx, destination)
    if not src.exists():
        raise ToolError(f"'{source}' does not exist.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"'{_rel(ctx, dst)}' already exists.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), dst)
    jid = ctx.store.add_journal(
        conversation_id=ctx.conversation_id,
        action="move",
        path=_rel(ctx, src),
        dest=_rel(ctx, dst),
        after_hash=file_hash(dst),
    )
    return ToolOutput(
        f"Moved {_rel(ctx, src)} to {_rel(ctx, dst)}.",
        ui={"journal_id": jid, "path": _rel(ctx, dst)},
    )


@tool(category="files", risk="confirm", summary="Delete {path}")
def delete_file(
    ctx: ToolContext,
    path: Annotated[str, "File or folder to delete, relative to the workspace"],
) -> ToolOutput:
    """Delete a file or folder from the workspace. It is kept in Bagley's journal so the user can
    restore it. Asks the user first."""
    target = resolve_entry(ctx, path)
    root = Path(ctx.config.workspace or ".").resolve()
    if target == root:
        raise ToolError("Refusing to delete the whole workspace.")
    if not target.exists():
        raise ToolError(f"'{path}' does not exist.")
    rel = _rel(ctx, target)
    backup = snapshot(ctx.config, target, move=True)
    jid = ctx.store.add_journal(
        conversation_id=ctx.conversation_id, action="delete", path=rel, backup=backup
    )
    return ToolOutput(
        f"Deleted {rel}. It can be restored from the chat.", ui={"journal_id": jid, "path": rel}
    )


@tool(category="files", summary="Create folder {path}")
def make_directory(
    ctx: ToolContext,
    path: Annotated[str, "Folder to create, relative to the workspace"],
) -> ToolOutput | str:
    """Create a folder (and any missing parents) in the workspace."""
    target = resolve(ctx, path)
    if target.exists():
        return f"{_rel(ctx, target)} already exists."
    target.mkdir(parents=True)
    rel = _rel(ctx, target)
    jid = ctx.store.add_journal(conversation_id=ctx.conversation_id, action="mkdir", path=rel)
    return ToolOutput(f"Created folder {rel}.", ui={"journal_id": jid, "path": rel})
