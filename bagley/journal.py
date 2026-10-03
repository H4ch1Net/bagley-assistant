"""Change journal: every file change Bagley makes is recorded and can be reverted.

Before a change, the previous version is copied (or, for deletes, moved) into
``<data dir>/journal``. A revert restores it, but only if the file still looks exactly the way
Bagley left it, so newer edits are never clobbered.
"""

from __future__ import annotations

import difflib
import hashlib
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.config import ServerConfig
    from bagley.store import Store

MAX_DIFF_LINES = 300


class JournalError(Exception):
    pass


def journal_dir(config: ServerConfig) -> Path:
    path = Path(config.data_dir) / "journal"
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot(config: ServerConfig, path: Path, *, move: bool = False) -> str | None:
    """Keep the current version of ``path`` (file or folder). Returns the backup name."""
    if not path.exists():
        return None
    name = uuid.uuid4().hex
    target = journal_dir(config) / name
    if move:
        shutil.move(str(path), target)
    elif path.is_dir():
        shutil.copytree(path, target)
    else:
        shutil.copy2(path, target)
    return name


def text_diff(old: str, new: str, rel: str) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
            n=2,
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        lines = [*lines[:MAX_DIFF_LINES], f"… {len(lines) - MAX_DIFF_LINES} more lines"]
    return "\n".join(lines)


def _workspace_path(config: ServerConfig, rel: str) -> Path:
    root = Path(config.workspace or ".").resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise JournalError("That path is outside the workspace.")
    return target


def revert(store: Store, config: ServerConfig, jid: int) -> dict[str, Any]:
    """Undo journal entry ``jid``. Returns the entry; raises ``JournalError`` if it can't."""
    entry = store.get_journal(jid)
    if not entry:
        raise JournalError("That change is not in the journal.")
    if entry["reverted_at"]:
        raise JournalError("That change was already reverted.")
    action = entry["action"]
    path = _workspace_path(config, entry["path"])
    backup = journal_dir(config) / entry["backup"] if entry["backup"] else None
    if backup and not backup.exists():
        raise JournalError("The saved copy for this change is missing.")

    if action in ("write", "edit"):
        if entry["after_hash"] and file_hash(path) != entry["after_hash"]:
            raise JournalError(f"{entry['path']} changed since; revert the newer change first.")
        if backup:
            shutil.copy2(backup, path)
        elif path.exists():
            path.unlink()
    elif action == "move":
        dest = _workspace_path(config, entry["dest"])
        if not dest.exists():
            raise JournalError(f"{entry['dest']} no longer exists.")
        if entry["after_hash"] and file_hash(dest) != entry["after_hash"]:
            raise JournalError(f"{entry['dest']} changed since it was moved.")
        if path.exists():
            raise JournalError(f"Something already exists at {entry['path']}.")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dest), path)
    elif action == "delete":
        if path.exists():
            raise JournalError(f"Something already exists at {entry['path']}.")
        assert backup is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup), path)
    elif action == "mkdir":
        if path.exists():
            if any(path.iterdir()):
                raise JournalError(f"{entry['path']} is not empty any more.")
            path.rmdir()
    else:
        raise JournalError(f"Can't revert a {action}.")
    store.mark_reverted(jid)
    return {**entry, "reverted": True}
