"""SQLite persistence for conversations, messages, memories and preferences."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    deleted_at  REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    reasoning       TEXT NOT NULL DEFAULT '',
    tool_calls      TEXT,
    tool_call_id    TEXT,
    name            TEXT,
    meta            TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    action          TEXT NOT NULL,
    path            TEXT NOT NULL,
    dest            TEXT,
    backup          TEXT,
    after_hash      TEXT,
    created_at      REAL NOT NULL,
    reverted_at     REAL
);
CREATE TABLE IF NOT EXISTS automations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    prompt          TEXT NOT NULL DEFAULT '',
    schedule        TEXT NOT NULL,
    target          TEXT,
    conversation_id TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    next_run        REAL,
    last_run        REAL,
    last_status     TEXT,
    last_result     TEXT,
    state           TEXT,
    created_at      REAL NOT NULL
);
"""

AUTOMATION_FIELDS = {
    "kind", "name", "prompt", "schedule", "target", "conversation_id", "enabled", "next_run",
    "last_run", "last_status", "last_result", "state",
}  # fmt: skip

# Soft-deleted conversations are kept this long so "Undo" works, then purged.
TRASH_RETENTION_SECONDS = 24 * 3600


def _now() -> float:
    return time.time()


class Store:
    """Small synchronous store. Every call is short, so a lock around one connection is enough."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript(SCHEMA)
            columns = {r[1] for r in self._db.execute("PRAGMA table_info(conversations)")}
            if "unread" not in columns:  # Added in schema 2.
                self._db.execute(
                    "ALTER TABLE conversations ADD COLUMN unread INTEGER NOT NULL DEFAULT 0"
                )
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.commit()
        self.purge_trash()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # Conversations ------------------------------------------------------------------------

    def create_conversation(self, title: str = "New chat") -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        now = _now()
        self._exec(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (cid, title, now, now),
        )
        return {"id": cid, "title": title, "created_at": now, "updated_at": now, "unread": 0}

    def get_conversation(self, cid: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT id, title, created_at, updated_at, unread FROM conversations "
            "WHERE id = ? AND deleted_at IS NULL",
            (cid,),
        )
        return dict(row) if row else None

    def list_conversations(self, query: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            rows = self._all(
                "SELECT c.id, c.title, c.created_at, c.updated_at, c.unread FROM conversations c "
                "WHERE c.deleted_at IS NULL AND (c.title LIKE ? ESCAPE '\\' OR EXISTS ("
                "  SELECT 1 FROM messages m WHERE m.conversation_id = c.id "
                "  AND m.role IN ('user', 'assistant') AND m.content LIKE ? ESCAPE '\\')) "
                "ORDER BY c.updated_at DESC LIMIT ?",
                (like, like, limit),
            )
        else:
            rows = self._all(
                "SELECT id, title, created_at, updated_at, unread FROM conversations "
                "WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows]

    def search_messages(
        self, query: str, *, exclude: str | None = None, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Past user and assistant messages matching the most words of ``query``, newest first."""
        words = [w for w in re.findall(r"\w{2,}", query.lower())][:8]
        if not words:
            return []
        likes = []
        for w in words:
            escaped = w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            likes.append(f"%{escaped}%")
        where = " OR ".join("lower(m.content) LIKE ? ESCAPE '\\'" for _ in likes)
        rows = self._all(
            "SELECT m.id, m.conversation_id, m.role, m.content, m.created_at, c.title "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.deleted_at IS NULL AND m.role IN ('user', 'assistant') "
            f"AND c.id != ? AND ({where}) ORDER BY m.id DESC LIMIT 300",
            (exclude or "", *likes),
        )
        scored = []
        for r in rows:
            text = r["content"].lower()
            score = sum(w in text for w in words)
            scored.append((score, r["id"], dict(r)))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [item for *_, item in scored[:limit]]

    def rename_conversation(self, cid: str, title: str) -> bool:
        title = " ".join(title.split())[:120] or "Untitled"
        cur = self._exec(
            "UPDATE conversations SET title = ? WHERE id = ? AND deleted_at IS NULL", (title, cid)
        )
        return cur.rowcount > 0

    def set_unread(self, cid: str, unread: bool) -> None:
        self._exec("UPDATE conversations SET unread = ? WHERE id = ?", (int(unread), cid))

    def touch_conversation(self, cid: str) -> None:
        self._exec("UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), cid))

    def delete_conversation(self, cid: str) -> bool:
        cur = self._exec(
            "UPDATE conversations SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (_now(), cid),
        )
        return cur.rowcount > 0

    def restore_conversation(self, cid: str) -> bool:
        cur = self._exec("UPDATE conversations SET deleted_at = NULL WHERE id = ?", (cid,))
        return cur.rowcount > 0

    def purge_trash(self, older_than: float = TRASH_RETENTION_SECONDS) -> None:
        self._exec(
            "DELETE FROM conversations WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (_now() - older_than,),
        )

    # Messages -----------------------------------------------------------------------------

    def add_message(
        self,
        cid: str,
        role: str,
        content: str = "",
        *,
        reasoning: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        cur = self._exec(
            "INSERT INTO messages (conversation_id, role, content, reasoning, tool_calls, "
            "tool_call_id, name, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                role,
                content,
                reasoning,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                name,
                json.dumps(meta) if meta else None,
                now,
            ),
        )
        self.touch_conversation(cid)
        return self._message_dict(
            {
                "id": cur.lastrowid,
                "conversation_id": cid,
                "role": role,
                "content": content,
                "reasoning": reasoning,
                "tool_calls": json.dumps(tool_calls) if tool_calls else None,
                "tool_call_id": tool_call_id,
                "name": name,
                "meta": json.dumps(meta) if meta else None,
                "created_at": now,
            }
        )

    def update_message(self, mid: int, *, content: str) -> None:
        self._exec("UPDATE messages SET content = ? WHERE id = ?", (content, mid))

    def list_messages(self, cid: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (cid,))
        return [self._message_dict(dict(r)) for r in rows]

    def delete_messages_from(self, cid: str, mid: int) -> None:
        """Delete message ``mid`` and everything after it (used by regenerate and edit)."""
        self._exec("DELETE FROM messages WHERE conversation_id = ? AND id >= ?", (cid, mid))

    @staticmethod
    def _message_dict(row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row["tool_calls"] = json.loads(row["tool_calls"]) if row.get("tool_calls") else None
        row["meta"] = json.loads(row["meta"]) if row.get("meta") else {}
        return row

    # Memories -----------------------------------------------------------------------------

    def add_memory(self, content: str) -> dict[str, Any]:
        content = " ".join(content.split())[:500]
        existing = self._one("SELECT * FROM memories WHERE lower(content) = lower(?)", (content,))
        if existing:
            return dict(existing)
        now = _now()
        cur = self._exec("INSERT INTO memories (content, created_at) VALUES (?, ?)", (content, now))
        return {"id": cur.lastrowid, "content": content, "created_at": now}

    def list_memories(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM memories ORDER BY id")]

    def delete_memory(self, mid: int) -> bool:
        return self._exec("DELETE FROM memories WHERE id = ?", (mid,)).rowcount > 0

    # Preferences --------------------------------------------------------------------------

    def get_preferences(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self._all("SELECT * FROM preferences")}

    def set_preferences(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT INTO preferences (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, json.dumps(v)) for k, v in values.items()],
            )
            self._db.commit()

    # Change journal ------------------------------------------------------------------------

    def add_journal(self, **fields: Any) -> int:
        keys = ["conversation_id", "action", "path", "dest", "backup", "after_hash"]
        cur = self._exec(
            f"INSERT INTO journal ({', '.join(keys)}, created_at) VALUES ({', '.join('?' * len(keys))}, ?)",
            (*(fields.get(k) for k in keys), _now()),
        )
        return int(cur.lastrowid or 0)

    def get_journal(self, jid: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM journal WHERE id = ?", (jid,))
        return dict(row) if row else None

    def mark_reverted(self, jid: int) -> None:
        self._exec("UPDATE journal SET reverted_at = ? WHERE id = ?", (_now(), jid))

    def reverted_ids(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        rows = self._all(
            f"SELECT id FROM journal WHERE reverted_at IS NOT NULL AND id IN ({','.join('?' * len(ids))})",
            tuple(ids),
        )
        return {r["id"] for r in rows}

    # Automations ---------------------------------------------------------------------------

    def create_automation(self, **fields: Any) -> dict[str, Any]:
        data = {k: v for k, v in fields.items() if k in AUTOMATION_FIELDS}
        cols = list(data)
        cur = self._exec(
            f"INSERT INTO automations ({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(cols))}, ?)",
            (*(self._encode(k, data[k]) for k in cols), _now()),
        )
        return self.get_automation(int(cur.lastrowid or 0)) or {}

    def update_automation(self, aid: int, **fields: Any) -> dict[str, Any] | None:
        data = {k: v for k, v in fields.items() if k in AUTOMATION_FIELDS}
        if data:
            sets = ", ".join(f"{k} = ?" for k in data)
            self._exec(
                f"UPDATE automations SET {sets} WHERE id = ?",
                (*(self._encode(k, v) for k, v in data.items()), aid),
            )
        return self.get_automation(aid)

    def get_automation(self, aid: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM automations WHERE id = ?", (aid,))
        return self._automation(row) if row else None

    def list_automations(self) -> list[dict[str, Any]]:
        return [self._automation(r) for r in self._all("SELECT * FROM automations ORDER BY id")]

    def due_automations(self, now: float) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM automations WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ? "
            "ORDER BY next_run",
            (now,),
        )
        return [self._automation(r) for r in rows]

    def delete_automation(self, aid: int) -> bool:
        return self._exec("DELETE FROM automations WHERE id = ?", (aid,)).rowcount > 0

    @staticmethod
    def _encode(key: str, value: Any) -> Any:
        if key == "state":
            return json.dumps(value) if value is not None else None
        if key == "enabled":
            return int(bool(value))
        return value

    @staticmethod
    def _automation(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["state"] = json.loads(data["state"]) if data.get("state") else {}
        return data
