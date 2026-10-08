"""``bagley cards``: review flashcards in the terminal, list decks, add a card.

    bagley cards                     review what is due in every deck
    bagley cards subnetting          review one deck
    bagley cards list                decks with their due and new cards
    bagley cards add DECK FRONT BACK add one card

A review shows the front, waits for Enter, shows the back and asks for a grade: 0-5, or a, h, g,
e for again, hard, good, easy (1, 3, 4, 5). Cards graded below 3 come back once at the end.
It uses the running server when there is one and the database directly otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

from bagley import study
from bagley.client import Client, ServerUnavailable
from bagley.commands.life import Ctos


class Remote:
    """Cards through the running server."""

    def __init__(self, client: Client) -> None:
        self.client = client

    def decks(self) -> list[dict[str, Any]]:
        return self.client.get("/api/flashcards/decks")

    def due(self, deck: str, limit: int) -> list[dict[str, Any]]:
        return self.client.get("/api/flashcards", deck=deck, due=1, limit=limit)

    def grade(self, card_id: int, quality: int) -> dict[str, Any]:
        return self.client.post(f"/api/flashcards/{card_id}/grade", {"quality": quality})

    def add(self, deck: str, front: str, back: str) -> dict[str, Any]:
        cards = [{"front": front, "back": back}]
        return self.client.post("/api/flashcards", {"deck": deck, "cards": cards})

    def close(self) -> None:
        self.client.close()


class Local:
    """Cards straight from the database, when no server runs."""

    def __init__(self) -> None:
        from bagley.config import ServerConfig
        from bagley.store import Store

        self.store = Store(ServerConfig.from_env().db_path)

    def decks(self) -> list[dict[str, Any]]:
        return study.list_decks(self.store)

    def due(self, deck: str, limit: int) -> list[dict[str, Any]]:
        return study.due_cards(self.store, deck or None, limit)

    def grade(self, card_id: int, quality: int) -> dict[str, Any]:
        return study.grade(self.store, card_id, quality)

    def add(self, deck: str, front: str, back: str) -> dict[str, Any]:
        return study.add_cards(self.store, deck, [{"front": front, "back": back}])

    def close(self) -> None:
        self.store.close()


def open_box() -> Remote | Local:
    client = Client()
    if client.available():
        return Remote(client)
    client.close()
    return Local()


def read_grade(s: Ctos) -> int | None:
    """A grade from the keyboard, or None to stop."""
    while True:
        answer = input(s.dim("  GRADE 0-5 // A H G E // Q QUITS › ")).strip().lower()
        if answer in study.KEY_GRADES:
            return study.KEY_GRADES[answer]
        if answer.isdigit() and 0 <= int(answer) <= 5:
            return int(answer)
        if answer in ("q", "quit", "exit"):
            return None
        print(f"  {s.bad('[ERR]')} {s.dim('0-5, or a (again) h (hard) g (good) e (easy)')}")


def review(box: Remote | Local, deck: str, limit: int, s: Ctos) -> int:
    cards = box.due(deck, limit)
    title = deck.upper() or "ALL DECKS"
    if not cards:
        print(s.bold(f"» REVIEW // {title}") + "  " + s.ok("NOTHING DUE"))
        return 0
    print(s.bold(f"» REVIEW // {title}") + f"  {s.bold(f'{len(cards):03d}')} {s.dim('DUE')}")
    queue = list(cards)
    retried: set[int] = set()
    reviewed = right = 0
    while queue:
        card = queue.pop(0)
        print()
        print(f"  {s.dim(f'[{reviewed + 1:03d}/{reviewed + 1 + len(queue):03d}]')} {card['deck']}")
        print(f"  {s.dim('Q')}  {s.bold(card['front'])}")
        try:
            input(s.dim("  ENTER REVEALS › "))
            print(f"  {s.dim('A')}  {s.body(card['back'])}")
            quality = read_grade(s)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if quality is None:
            break
        updated = box.grade(card["id"], quality)
        reviewed += 1
        due = datetime.fromtimestamp(updated["due"]).strftime("%d%m%y")
        days = f"{updated['interval']:03d} DAYS"
        if quality >= 3:
            right += 1
            print(f"  {s.ok('[OK]')} {s.dim(f'NEXT {due} // {days}')}")
        else:
            print(f"  {s.bad('[AGAIN]')} {s.dim(f'NEXT {due} // {days}')}")
            if card["id"] not in retried:  # Once more before the session ends.
                retried.add(card["id"])
                queue.append(card)
    print()
    print(
        s.bold("» SESSION END")
        + f"  {s.bold(f'{reviewed:03d}')} {s.dim('REVIEWED')}"
        + f"  {s.ok(f'{right:03d}')} {s.dim('OK')}"
        + f"  {s.bad(f'{reviewed - right:03d}')} {s.dim('AGAIN')}"
    )
    return 0


def list_decks(box: Remote | Local, as_json: bool, s: Ctos) -> int:
    decks = box.decks()
    if as_json:
        print(json.dumps(decks))
        return 0
    print(s.bold(f"» DECKS // {len(decks):03d}"))
    for d in decks:
        due = s.ok(f"{d['due']:03d}") if d["due"] else s.dim("000")
        total = s.bold(f"{d['total']:03d}")
        print(
            f"  {s.body(d['deck'][:24].upper().ljust(24))} {total} {s.dim('CARDS')}  "
            f"{due} {s.dim('DUE')}  {d['new']:03d} {s.dim('NEW')}"
        )
    if not decks:
        print("  " + s.dim("NO CARDS // bagley cards add DECK FRONT BACK"))
    return 0


def add_card(box: Remote | Local, words: list[str], s: Ctos) -> int:
    if len(words) != 3:
        print('Usage: bagley cards add DECK "front" "back"', file=sys.stderr)
        return 2
    result = box.add(*words)
    if result["added"]:
        print(f"{s.ok('[OK]')} ADDED // {result['deck']} #{result['ids'][0]}")
    else:
        print(f"{s.warn('[WARN]')} ALREADY IN {result['deck']}")
    return 0


def cmd_cards(args: argparse.Namespace) -> int:
    s = Ctos(sys.stdout)
    words = list(args.words)
    try:
        box = open_box()
    except Exception as exc:
        print(f"{s.bad('[CRIT]')} {exc}", file=sys.stderr)
        return 1
    try:
        if words and words[0] == "list":
            return list_decks(box, args.json, s)
        if words and words[0] == "add":
            return add_card(box, words[1:], s)
        return review(box, " ".join(words), max(1, min(args.limit, 500)), s)
    except (ServerUnavailable, study.StudyError) as exc:
        print(f"{s.bad('[CRIT]')} {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        box.close()


def register(sub: argparse._SubParsersAction) -> None:
    cards = sub.add_parser("cards", help="Review flashcards, list decks or add a card")
    cards.add_argument(
        "words", nargs="*", metavar="DECK | list | add DECK FRONT BACK", help="What to do"
    )
    cards.add_argument("-n", "--limit", type=int, default=20, help="Cards per review (20)")
    cards.add_argument("--json", action="store_true", help="List decks as JSON")
    cards.set_defaults(func=cmd_cards)
