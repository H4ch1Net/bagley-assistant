"""Flashcards: decks, cards due for review, grading, and CSV export and import.

Every change is broadcast to open windows as ``{"type": "flashcards.changed"}``.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bagley import study
from bagley.api import runtime

router = APIRouter()


class CardIn(BaseModel):
    front: str = Field(min_length=1, max_length=study.MAX_FRONT)
    back: str = Field(min_length=1, max_length=study.MAX_BACK)


class CardsBody(BaseModel):
    deck: str = Field(default=study.DEFAULT_DECK, max_length=200)
    cards: list[CardIn] = Field(min_length=1, max_length=study.MAX_CARDS)
    source: str | None = Field(default=None, max_length=500)  # A note the cards came from.


class GradeBody(BaseModel):
    quality: int = Field(ge=0, le=5)


async def _changed(request: Request) -> None:
    await runtime(request).broadcast({"type": "flashcards.changed"})


@router.get("/api/flashcards/decks")
async def decks(request: Request) -> list[dict[str, Any]]:
    return study.list_decks(runtime(request).store)


@router.get("/api/flashcards")
async def cards(
    request: Request, deck: str = "", due: bool = False, limit: int = 500
) -> list[dict[str, Any]]:
    return study.list_cards(runtime(request).store, deck or None, due_only=due, limit=limit)


@router.post("/api/flashcards", status_code=201)
async def add(body: CardsBody, request: Request) -> dict[str, Any]:
    try:
        result = study.add_cards(
            runtime(request).store,
            body.deck,
            [c.model_dump() for c in body.cards],
            source=body.source,
        )
    except study.StudyError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _changed(request)
    return result


@router.post("/api/flashcards/{card_id}/grade")
async def grade(card_id: int, body: GradeBody, request: Request) -> dict[str, Any]:
    store = runtime(request).store
    if study.get_card(store, card_id) is None:
        raise HTTPException(404, "No such flashcard.")
    card = study.grade(store, card_id, body.quality)
    await _changed(request)
    return card


@router.delete("/api/flashcards/{card_id}")
async def delete(card_id: int, request: Request) -> dict[str, Any]:
    if not study.delete_card(runtime(request).store, card_id):
        raise HTTPException(404, "No such flashcard.")
    await _changed(request)
    return {"ok": True}


@router.get("/api/flashcards/export")
async def export(request: Request, deck: str = "") -> Response:
    text = study.export_csv(runtime(request).store, deck or None)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", deck).strip("-").lower() or "flashcards"
    return Response(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


@router.post("/api/flashcards/import")
async def import_cards(request: Request, deck: str = "") -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > study.MAX_IMPORT_BYTES:
            raise HTTPException(413, "CSV file too large.")
    try:
        result = study.import_csv(
            runtime(request).store, bytes(body).decode("utf-8", "replace"), deck or None
        )
    except study.StudyError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result["added"]:
        await _changed(request)
    return result
