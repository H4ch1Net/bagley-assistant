"""Study tools: flashcards with spaced repetition, quiz material from the user's notes and timed
study sessions. Study mode loads them from the first message."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from bagley import study
from bagley.automations import ScheduleError
from bagley.policy import changes_state, reads_private
from bagley.toolroute import register_group
from bagley.tools import ToolContext, ToolError, tool

register_group(
    "study",
    "flashcards, quizzes and study sessions",
    r"\b(flashcard|cards?\b|quiz|revise|revision|study|exam|learn|memori|test me|review)",
)
reads_private("study_material", "due_flashcards", "list_decks")
changes_state("add_flashcards", "grade_flashcard", "start_study_session")


async def _changed(ctx: ToolContext) -> None:
    if ctx.runtime is not None:
        await ctx.runtime.broadcast({"type": "flashcards.changed"})


def _brief(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": card["id"],
        "deck": card["deck"],
        "front": card["front"],
        "back": card["back"],
        "new": card["new"],
        "reps": card["reps"],
        "lapses": card["lapses"],
    }


@tool(category="study", summary="Save flashcards to {deck}")
async def add_flashcards(
    ctx: ToolContext,
    cards: Annotated[
        list[dict], "Cards as objects with 'front' (a question or term) and 'back' (the answer)"
    ],
    deck: Annotated[str, "Deck name, e.g. 'Subnetting' or 'Active Directory'"] = "General",
) -> str:
    """Save flashcards for spaced-repetition review. Keep each card to one fact: a short
    question on the front, a short answer on the back. Cards already in the deck are skipped."""
    try:
        result = study.add_cards(ctx.store, deck, cards)
    except study.StudyError as exc:
        raise ToolError(str(exc)) from exc
    if result["added"]:
        await _changed(ctx)
    text = f"Added {result['added']} card(s) to {result['deck']}."
    if result["duplicates"]:
        text += f" Skipped {len(result['duplicates'])} already in the deck."
    return text


@tool(category="study", summary="Get due flashcards")
def due_flashcards(
    ctx: ToolContext,
    deck: Annotated[str, "Deck name; empty for every deck"] = "",
    limit: Annotated[int, "How many cards (1-50)"] = 10,
) -> dict[str, Any] | str:
    """Flashcards due for review, oldest first. Quiz the user one card at a time: show the
    front, wait for their answer, reveal the back, then record how well they knew it with
    grade_flashcard."""
    cards = study.due_cards(ctx.store, deck or None, max(1, min(limit, 50)))
    if not cards:
        decks = study.list_decks(ctx.store)
        if not decks:
            return "There are no flashcards yet. Offer to make some with add_flashcards."
        return "Nothing is due right now. " + ", ".join(
            f"{d['deck']}: {d['total']} cards" for d in decks
        )
    return {"due": [_brief(c) for c in cards], "decks": study.list_decks(ctx.store)}


@tool(category="study", summary="Grade flashcard #{card_id}: {quality}")
async def grade_flashcard(
    ctx: ToolContext,
    card_id: Annotated[int, "The card's id from due_flashcards"],
    quality: Annotated[
        int,
        "How well the user knew it: 0 blank, 1 wrong, 2 wrong but close, 3 right with effort, "
        "4 right after a pause, 5 instant",
    ],
) -> str:
    """Record how well the user answered a flashcard; this schedules its next review."""
    try:
        card = study.grade(ctx.store, card_id, quality)
    except study.StudyError as exc:
        raise ToolError(str(exc)) from exc
    await _changed(ctx)
    when = datetime.fromtimestamp(card["due"]).strftime("%a %d %b")
    days = card["interval"]
    return (
        f"Card #{card_id} graded {quality}. Next review {when} (in {days} day{'s' * (days != 1)})."
    )


@tool(category="study", summary="List flashcard decks")
def list_decks(ctx: ToolContext) -> list[dict[str, Any]] | str:
    """The user's flashcard decks with how many cards each has, how many are due and how many
    are new."""
    return study.list_decks(ctx.store) or "There are no flashcards yet."


@tool(category="study", summary="Find study material on “{topic}”")
async def study_material(
    ctx: ToolContext,
    topic: Annotated[str, "What to study, e.g. 'OSPF areas' or 'Kerberos'"],
    limit: Annotated[int, "Number of passages (1-10)"] = 5,
) -> dict[str, Any] | str:
    """Passages about a topic from the user's own notes and course documents (their knowledge
    base), to build quiz questions or flashcards from. Base questions on these passages and say
    which note each comes from."""
    kb = getattr(ctx.runtime, "knowledge", None)
    if kb is None:
        raise ToolError("The knowledge base is not available here.")
    hits = await kb.search(topic, max(1, min(limit, 10)))
    if not hits:
        return (
            f"Nothing in the user's notes matches “{topic}”. Say so, and offer to quiz them from "
            "general knowledge instead."
        )
    return {"topic": topic, "passages": hits}


@tool(category="study", summary="Start a {minutes}-minute study session")
async def start_study_session(
    ctx: ToolContext,
    minutes: Annotated[int, "Length of the focus block in minutes (5-180)"] = 25,
    topic: Annotated[str, "What the user is studying"] = "",
) -> str:
    """Start a timed study session: Bagley reminds the user when the focus block ends and again
    when the break is over. Use it when they want to study or revise for a set time."""
    scheduler = getattr(ctx.runtime, "scheduler", None)
    if scheduler is None:
        raise ToolError("Reminders are not available here.")
    if not 5 <= minutes <= 180:
        raise ToolError("A focus block lasts 5 to 180 minutes.")
    pause = max(5, minutes // 5)
    subject = " ".join(topic.split())[:60]
    label = f" on {subject}" if subject else ""
    try:
        focus = scheduler.create(
            "reminder",
            f"Focus block{label}"[:60],
            f"in {minutes} minutes",
            prompt=f"Focus block{label} done. Take a {pause}-minute break.",
            conversation_id=ctx.conversation_id,
        )
        scheduler.create(
            "reminder",
            "Break over",
            f"in {minutes + pause} minutes",
            prompt=f"Break over. Back to {subject or 'studying'}.",
            conversation_id=ctx.conversation_id,
        )
    except ScheduleError as exc:
        raise ToolError(str(exc)) from exc
    await ctx.runtime.broadcast({"type": "automations.changed"})
    ends = datetime.fromtimestamp(focus["next_run"])
    back = ends + timedelta(minutes=pause)
    return (
        f"Study session started{label}: focus until {ends:%H:%M}, a {pause}-minute break, back at "
        f"{back:%H:%M}. Both reminders will appear in this chat."
    )
