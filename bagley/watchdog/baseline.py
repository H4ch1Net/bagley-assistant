"""What the watchdog has seen before: known devices, listening ports, Tailscale peers, and
caches such as the Arch security tracker.

The first run records a baseline and reports ``BASELINE RECORDED``. Later runs flag anything
missing from it as new. A new item is added to the baseline at once but stays flagged for
``NEW_FOR`` seconds, so asking twice gives the same answer and the next morning briefing still
shows it. ``reset`` forgets a section's baseline; the next run records it again.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchdog_baseline (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

NEW_FOR = 48 * 3600  # Long enough to reach the next daily briefing whatever ran in between.
MAX_ITEMS = 2000  # Per key; the least recently seen are dropped first.
CACHE = "cache."  # Keys under this prefix are caches, never reset.


class Baseline:
    """Key-value rows in the ``watchdog_baseline`` table. Values are JSON."""

    def __init__(self, store: Store) -> None:
        self.store = store
        store.ensure_schema(SCHEMA)

    def get(self, key: str, default: Any = None) -> Any:
        row = self.store.query_one("SELECT value FROM watchdog_baseline WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set(self, key: str, value: Any, now: float | None = None) -> None:
        self.store.execute(
            "INSERT INTO watchdog_baseline (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, separators=(",", ":")), time.time() if now is None else now),
        )

    def delete(self, key: str) -> None:
        self.store.execute("DELETE FROM watchdog_baseline WHERE key = ?", (key,))

    def keys(self, prefix: str = "") -> list[dict[str, Any]]:
        """Stored keys starting with ``prefix``, most recently updated first."""
        return self.store.query(
            "SELECT key, updated_at FROM watchdog_baseline WHERE substr(key, 1, ?) = ? "
            "ORDER BY updated_at DESC",
            (len(prefix), prefix),
        )

    def prune(self, prefix: str, keep: int) -> None:
        """Keep only the ``keep`` most recently updated keys under ``prefix``."""
        for row in self.keys(prefix)[keep:]:
            self.delete(row["key"])

    def track(
        self, key: str, items: dict[str, dict[str, Any]], now: float
    ) -> tuple[bool, list[str]]:
        """Compare ``items`` (id -> display fields) with the stored ones.

        Returns ``(recorded, new)``: ``recorded`` when there was no baseline yet, in which case
        everything is stored and nothing is new. Otherwise ``new`` lists the ids that are not
        part of the original baseline and were first seen less than ``NEW_FOR`` ago."""
        stored = self.get(key)
        if not isinstance(stored, dict) or not isinstance(stored.get("items"), dict):
            known = {k: {**v, "first_seen": now, "last_seen": now} for k, v in items.items()}
            self.set(key, {"recorded_at": now, "items": known}, now)
            return True, []
        known = stored["items"]
        new: list[str] = []
        for item_id, fields in items.items():
            entry = known.get(item_id)
            if entry is None:
                entry = known[item_id] = {"first_seen": now, "new": True}
            entry.update(fields)
            entry["last_seen"] = now
            if entry.get("new") and now - float(entry.get("first_seen", now)) < NEW_FOR:
                new.append(item_id)
        if len(known) > MAX_ITEMS:
            ranked = sorted(known, key=lambda k: known[k].get("last_seen", 0), reverse=True)
            known = {k: known[k] for k in ranked[:MAX_ITEMS]}
        self.set(key, {"recorded_at": stored.get("recorded_at", now), "items": known}, now)
        return False, new

    def reset(self, sections: Iterable[str] | None = None) -> list[str]:
        """Forget the baseline of ``sections`` (all sections when None). Caches are kept.
        Returns the keys removed."""
        rows = [r["key"] for r in self.keys() if not r["key"].startswith(CACHE)]
        if sections is not None:
            wanted = tuple(sections)
            rows = [k for k in rows if any(k == s or k.startswith(f"{s}.") for s in wanted)]
        for key in rows:
            self.delete(key)
        return rows


def reset(store: Store, sections: Iterable[str] | None = None) -> list[str]:
    """Forget known devices, ports and peers so the next run records them again."""
    return Baseline(store).reset(sections)
