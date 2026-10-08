"""Flashcards with SM-2 spaced repetition.

Cards live in the ``flashcards`` table of the main database, grouped in decks. Each review is
graded 0-5 (0 blackout, 3 correct with effort, 5 perfect). A grade below 3 counts a lapse and
starts the card over with a one-day interval; otherwise the interval grows 1, 6, then by the
card's ease, which a grade nudges up or down (never below 1.3). Cards fall due at the start of
their day, so one review session a day catches everything.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import time
import weakref
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bagley.store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS flashcards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    deck        TEXT NOT NULL,
    front       TEXT NOT NULL,
    back        TEXT NOT NULL,
    ease        REAL NOT NULL DEFAULT 2.5,
    interval    INTEGER NOT NULL DEFAULT 0,
    reps        INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0,
    due         REAL NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    source      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_flashcards_front
    ON flashcards(deck COLLATE NOCASE, front COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_flashcards_due ON flashcards(due);
"""

DEFAULT_DECK = "General"
MAX_DECK = 60
MAX_FRONT = 1_000
MAX_BACK = 4_000
MAX_CARDS = 200  # Per add call.
MAX_IMPORT_BYTES = 2_000_000
MAX_IMPORT_ROWS = 5_000
MIN_EASE = 1.3
KEY_GRADES = {"a": 1, "h": 3, "g": 4, "e": 5}  # Again, hard, good, easy.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")  # Spreadsheets run cells that start so.

_ready: weakref.WeakSet[Store] = weakref.WeakSet()


class StudyError(ValueError):
    pass


def ensure(store: Store) -> Store:
    if store not in _ready:
        store.ensure_schema(SCHEMA)
        _ready.add(store)
    return store


def _clean(text: Any, limit: int) -> str:
    return re.sub(r"[ \t]+", " ", str(text or "")).strip()[:limit]


def deck_name(name: str | None) -> str:
    return " ".join(str(name or "").split())[:MAX_DECK] or DEFAULT_DECK


def _day_start(moment: datetime) -> float:
    local = moment.astimezone().replace(tzinfo=None)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone().timestamp()


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now()).astimezone()


def _card(row: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    return {**row, "new": row["interval"] == 0, "is_due": row["due"] <= now}


# SM-2 -----------------------------------------------------------------------------------------


def schedule(card: dict[str, Any], quality: int, now: datetime | None = None) -> dict[str, Any]:
    """The card's next ease, interval, repetitions, lapses and due time after a review."""
    if not isinstance(quality, int) or isinstance(quality, bool) or not 0 <= quality <= 5:
        raise StudyError("Grade from 0 (forgot) to 5 (perfect).")
    local = _now(now).replace(tzinfo=None)  # Count days on the wall clock.
    ease, interval = float(card["ease"]), int(card["interval"])
    reps, lapses = int(card["reps"]), int(card["lapses"])
    if quality >= 3:
        interval = 1 if reps == 0 else 6 if reps == 1 else max(1, round(interval * ease))
        reps += 1
    else:
        reps, interval, lapses = 0, 1, lapses + 1
    ease = max(MIN_EASE, ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    due = _day_start((local + timedelta(days=interval)).astimezone())
    return {
        "ease": round(ease, 4),
        "interval": interval,
        "reps": reps,
        "lapses": lapses,
        "due": due,
    }


# Cards ----------------------------------------------------------------------------------------


def _pair(item: Any) -> tuple[str, str]:
    """Front and back from {front, back} (or question/answer, q/a, term/definition)."""
    if not isinstance(item, dict):
        raise StudyError("Each card needs a front and a back.")
    keys = {str(k).lower(): v for k, v in item.items()}
    front = next((keys[k] for k in ("front", "question", "q", "term") if keys.get(k)), "")
    back = next((keys[k] for k in ("back", "answer", "a", "definition") if keys.get(k)), "")
    front, back = _clean(front, MAX_FRONT), _clean(back, MAX_BACK)
    if not front or not back:
        raise StudyError("Each card needs a front and a back.")
    return front, back


def add_cards(
    store: Store,
    deck: str | None,
    cards: list[Any],
    *,
    source: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add cards to a deck. A card whose front is already in the deck is skipped."""
    ensure(store)
    if not isinstance(cards, list) or not cards:
        raise StudyError("Give at least one card.")
    if len(cards) > MAX_CARDS:
        raise StudyError(f"Add at most {MAX_CARDS} cards at a time.")
    pairs = [_pair(c) for c in cards]
    name = deck_name(deck)
    existing = store.query_one(
        "SELECT deck FROM flashcards WHERE deck = ? COLLATE NOCASE LIMIT 1", (name,)
    )
    name = existing["deck"] if existing else name  # Keep the deck's first spelling.
    stamp = _now(now).timestamp()
    added: list[int] = []
    duplicates: list[str] = []
    for front, back in pairs:
        try:
            cur = store.execute(
                "INSERT INTO flashcards (deck, front, back, due, created_at, updated_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, front, back, stamp, stamp, stamp, (source or "")[:500] or None),
            )
        except sqlite3.IntegrityError:
            duplicates.append(front)
            continue
        added.append(int(cur.lastrowid or 0))
    return {"deck": name, "added": len(added), "ids": added, "duplicates": duplicates}


def get_card(store: Store, card_id: int) -> dict[str, Any] | None:
    row = ensure(store).query_one("SELECT * FROM flashcards WHERE id = ?", (card_id,))
    return _card(row) if row else None


def list_cards(
    store: Store,
    deck: str | None = None,
    *,
    due_only: bool = False,
    limit: int = 500,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Cards in a deck (every deck when ``deck`` is empty). Due cards come oldest due first."""
    ensure(store)
    stamp = _now(now).timestamp()
    where, params = [], []
    if deck and deck.strip():
        where.append("deck = ? COLLATE NOCASE")
        params.append(deck_name(deck))
    if due_only:
        where.append("due <= ?")
        params.append(stamp)
    sql = "SELECT * FROM flashcards"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY due, id" if due_only else " ORDER BY deck, id"
    rows = store.query(sql + " LIMIT ?", (*params, max(1, min(limit, 5000))))
    return [_card(r, stamp) for r in rows]


def due_cards(
    store: Store, deck: str | None = None, limit: int = 20, now: datetime | None = None
) -> list[dict[str, Any]]:
    return list_cards(store, deck, due_only=True, limit=limit, now=now)


def list_decks(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every deck with its number of cards, cards due now and cards never reviewed."""
    ensure(store)
    return store.query(
        "SELECT deck, count(*) AS total, sum(due <= ?) AS due, sum(interval = 0) AS new "
        "FROM flashcards GROUP BY deck COLLATE NOCASE ORDER BY deck COLLATE NOCASE",
        (_now(now).timestamp(),),
    )


def grade(store: Store, card_id: int, quality: int, now: datetime | None = None) -> dict[str, Any]:
    """Record a review and return the updated card."""
    card = get_card(store, card_id)
    if card is None:
        raise StudyError(f"There is no flashcard #{card_id}.")
    nxt = schedule(card, quality, now)
    values = [nxt[k] for k in ("ease", "interval", "reps", "lapses", "due")]
    store.execute(
        "UPDATE flashcards SET ease = ?, interval = ?, reps = ?, lapses = ?, due = ?, "
        "updated_at = ? WHERE id = ?",
        (*values, _now(now).timestamp(), card_id),
    )
    updated = get_card(store, card_id)
    assert updated is not None
    return updated


def delete_card(store: Store, card_id: int) -> bool:
    return ensure(store).execute("DELETE FROM flashcards WHERE id = ?", (card_id,)).rowcount > 0


# CSV --------------------------------------------------------------------------------------------


def _escape(cell: str) -> str:
    return "'" + cell if cell.startswith(FORMULA_START) else cell


def _unescape(cell: str) -> str:
    return cell[1:] if cell.startswith("'") and cell[1:].startswith(FORMULA_START) else cell


def export_csv(store: Store, deck: str | None = None) -> str:
    """Cards as CSV with a ``front,back,deck`` header."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["front", "back", "deck"])
    for card in list_cards(store, deck, limit=5000):
        writer.writerow([_escape(card["front"]), _escape(card["back"]), _escape(card["deck"])])
    return out.getvalue()


def import_csv(store: Store, text: str, deck: str | None = None) -> dict[str, Any]:
    """Add cards from CSV rows of ``front,back[,deck]`` (a header row is optional). ``deck``
    puts every card in that deck; otherwise the third column or the default deck is used."""
    if len(text.encode()) > MAX_IMPORT_BYTES:
        raise StudyError(f"CSV files up to {MAX_IMPORT_BYTES // 1_000_000} MB can be imported.")
    text = text.lstrip("\ufeff")
    first = text.split("\n", 1)[0]
    # Bagley writes commas; Anki and Quizlet exports use tabs, some spreadsheets semicolons.
    delimiter = next((d for d in ("\t", ";") if d in first and "," not in first), ",")
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if rows and [c.strip().lower() for c in rows[0][:2]] == ["front", "back"]:
        rows = rows[1:]
    if len(rows) > MAX_IMPORT_ROWS:
        raise StudyError(f"Import at most {MAX_IMPORT_ROWS} cards at a time.")
    by_deck: dict[str, list[dict[str, str]]] = {}
    skipped = 0
    for row in rows:
        cells = [_unescape(c) for c in row]
        if len(cells) < 2 or not cells[0].strip() or not cells[1].strip():
            skipped += 1
            continue
        name = deck_name(deck if deck and deck.strip() else (cells[2] if len(cells) > 2 else ""))
        by_deck.setdefault(name, []).append({"front": cells[0], "back": cells[1]})
    added, duplicates = 0, 0
    for name, cards in by_deck.items():
        for start in range(0, len(cards), MAX_CARDS):
            result = add_cards(store, name, cards[start : start + MAX_CARDS])
            added += result["added"]
            duplicates += len(result["duplicates"])
    return {"added": added, "duplicates": duplicates, "skipped": skipped, "decks": list(by_deck)}
