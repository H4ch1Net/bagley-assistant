from __future__ import annotations

import io
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from bagley import cli, study
from bagley.agent import Agent, RunRequest
from bagley.policy import CHANGES_STATE, READS_PRIVATE, UnattendedPolicy
from bagley.server import create_app
from bagley.store import Store
from bagley.toolroute import select_tools
from tests.demo_stack import free_port
from tests.mock_llm import Reply

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 8, 15, 0).astimezone()


def midnight(days: int) -> float:
    day = (NOW.replace(tzinfo=None) + timedelta(days=days)).replace(hour=0, minute=0)
    return day.astimezone().timestamp()


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


# SM-2 -------------------------------------------------------------------------------------------


def test_sm2_schedule():
    card = {"ease": 2.5, "interval": 0, "reps": 0, "lapses": 0}
    steps = []
    for quality in (4, 5, 3, 1, 0, 4):
        card = study.schedule(card, quality, NOW)
        steps.append((card["interval"], card["reps"], card["lapses"], card["ease"]))
    assert steps == [
        (1, 1, 0, 2.5),  # First success: one day; a 4 keeps the ease.
        (6, 2, 0, 2.6),  # Second: six days; a 5 raises the ease.
        (16, 3, 0, 2.46),  # Then interval x ease (6 x 2.6), and a 3 lowers it.
        (1, 0, 1, 1.92),  # A lapse: back to one day.
        (1, 0, 2, 1.3),  # Ease never drops below 1.3.
        (1, 1, 2, 1.3),
    ]
    assert study.schedule(card, 5, NOW)["due"] == midnight(6)  # Due at the start of the day.
    for bad in (-1, 6, True):
        with pytest.raises(study.StudyError, match="0 \\(forgot\\) to 5"):
            study.schedule(card, bad, NOW)


def test_add_dedupes_by_deck_and_front(store):
    first = study.add_cards(
        store, "Networking", [{"front": "What does DNS do?", "back": "Resolves names"}]
    )
    assert first["added"] == 1 and first["deck"] == "Networking"
    again = study.add_cards(
        store,
        "networking ",  # Same deck, other spelling: the first spelling stays.
        [
            {"front": "what does  DNS do?", "back": "Other"},
            {"question": "Port of SSH?", "answer": "22"},
        ],
    )
    assert again["deck"] == "Networking" and again["added"] == 1
    assert again["duplicates"] == ["what does DNS do?"]
    other = study.add_cards(store, "Linux", [{"front": "What does DNS do?", "back": "x"}])
    assert other["added"] == 1
    assert [(d["deck"], d["total"]) for d in study.list_decks(store)] == [
        ("Linux", 1),
        ("Networking", 2),
    ]
    with pytest.raises(study.StudyError, match="front and a back"):
        study.add_cards(store, "X", [{"front": "only a front"}])
    with pytest.raises(study.StudyError, match="at least one"):
        study.add_cards(store, "X", [])
    assert study.add_cards(store, "", [{"front": "a", "back": "b"}])["deck"] == "General"


def test_due_ordering_and_deck_counts(store):
    earlier = NOW - timedelta(days=3)
    ids = study.add_cards(
        store, "Net", [{"front": f"Q{i}", "back": f"A{i}"} for i in range(4)], now=earlier
    )["ids"]
    study.grade(store, ids[0], 5, now=earlier)  # Due again in a day: overdue by now.
    study.grade(store, ids[1], 5, now=NOW)  # Due tomorrow.
    study.grade(store, ids[2], 1, now=earlier - timedelta(days=1))  # Lapsed, due long ago.
    due = study.due_cards(store, "net", now=NOW)
    assert [c["id"] for c in due] == [ids[2], ids[3], ids[0]]  # Most overdue first.
    assert due[1]["new"] is True and due[0]["new"] is False and due[2]["new"] is False
    assert study.due_cards(store, "Net", limit=1, now=NOW)[0]["id"] == ids[2]
    assert study.list_decks(store, now=NOW) == [{"deck": "Net", "total": 4, "due": 3, "new": 1}]
    tomorrow = NOW + timedelta(days=1)
    assert len(study.due_cards(store, now=tomorrow.replace(hour=0, minute=1))) == 4
    assert study.delete_card(store, ids[3]) and not study.delete_card(store, ids[3])
    with pytest.raises(study.StudyError, match="no flashcard #999"):
        study.grade(store, 999, 3)


def test_csv_round_trip(store):
    cards = [
        {"front": "Comma, inside", "back": 'Quote "here"'},
        {"front": "Multi\nline", "back": "Ünïcode ✓"},
        {"front": "=SUM(A1:A2)", "back": "-5"},
        {"front": "'quoted", "back": "+1"},
    ]
    study.add_cards(store, "Tricky", cards)
    study.add_cards(store, "Other", [{"front": "x", "back": "y"}])
    text = study.export_csv(store, "Tricky")
    assert text.splitlines()[0] == "front,back,deck"
    assert "'=SUM(A1:A2)" in text  # Spreadsheets don't run it as a formula.

    target = Store(":memory:")
    try:
        result = study.import_csv(target, text)
        assert result == {"added": 4, "duplicates": 0, "skipped": 0, "decks": ["Tricky"]}
        back = {(c["front"], c["back"], c["deck"]) for c in study.list_cards(target)}
        assert back == {(c["front"], c["back"], "Tricky") for c in cards}
        assert study.import_csv(target, text)["duplicates"] == 4
        renamed = study.import_csv(target, "Port of DNS?\t53\nbroken row\n", deck="Ports")
        assert renamed == {"added": 1, "duplicates": 0, "skipped": 1, "decks": ["Ports"]}
    finally:
        target.close()


# Tools ------------------------------------------------------------------------------------------


async def test_flashcard_tools_in_a_chat(make_runtime, mock, recorder):
    rt = make_runtime()
    cards = [
        {"front": "Default port of HTTPS?", "back": "443"},
        {"front": "What is a /26 mask?", "back": "255.255.255.192"},
    ]
    mock.script = [
        Reply(tool_calls=[("add_flashcards", {"cards": cards, "deck": "Networking"})]),
        Reply(tool_calls=[("due_flashcards", {"deck": "Networking"})]),
        Reply(tool_calls=[("grade_flashcard", {"card_id": 1, "quality": 5})]),
        Reply(tool_calls=[("list_decks", {})]),
        Reply(text="Saved and reviewed."),
    ]
    await Agent(rt).run(
        RunRequest(text="make flashcards for these and quiz me"), recorder.emit, recorder.approve
    )
    assert recorder.asked == []  # Saving cards doesn't need approval.
    results = [e["result"] for e in recorder.of("tool.end")]
    assert results[0] == "Added 2 card(s) to Networking."
    due = json.loads(results[1])
    assert [c["front"] for c in due["due"]] == [c["front"] for c in cards]
    assert results[2].startswith("Card #1 graded 5. Next review") and "(in 1 day)" in results[2]
    assert json.loads(results[3]) == [{"deck": "Networking", "total": 2, "due": 1, "new": 1}]
    assert {"add_flashcards", "grade_flashcard", "start_study_session"} <= CHANGES_STATE
    assert {"study_material", "due_flashcards"} <= READS_PRIVATE
    for name in ("add_flashcards", "due_flashcards", "grade_flashcard", "list_decks"):
        tool = rt.registry.get(name)
        assert tool.category == "study" and tool.risk == "safe"


async def test_unattended_runs_cannot_change_cards(make_runtime):
    rt = make_runtime()
    tool = rt.registry.get("add_flashcards")
    assert "scheduled runs can't change" in UnattendedPolicy().check(tool, {})


async def test_bad_cards_are_reported(make_runtime):
    rt = make_runtime()
    tool = rt.registry.get("add_flashcards")
    with pytest.raises(Exception, match="front and a back"):
        await tool.invoke({"cards": [{"front": "Q"}], "deck": "X"}, rt.tool_context())
    text = await rt.registry.get("due_flashcards").invoke({}, rt.tool_context())
    assert text.startswith("There are no flashcards yet")


async def test_study_material_searches_notes(make_runtime, tmp_path):
    notes = tmp_path / "Course"
    notes.mkdir()
    (notes / "ospf.md").write_text(
        "# OSPF\n\nOSPF areas reduce LSA flooding. Area 0 is the backbone."
    )
    rt = make_runtime(knowledge_folders=[str(notes)], embedding_model="off")
    await rt.knowledge.reindex()
    tool = rt.registry.get("study_material")
    result = json.loads(await tool.invoke({"topic": "OSPF areas"}, rt.tool_context()))
    assert result["passages"][0]["path"] == "Course/ospf.md"
    assert "backbone" in result["passages"][0]["text"]
    empty = await tool.invoke({"topic": "kerberos tickets"}, rt.tool_context())
    assert empty.startswith("Nothing in the user's notes matches")


async def test_study_session_sets_two_reminders(make_runtime):
    rt = make_runtime()
    chat = rt.store.create_conversation("Revision", mode="study")
    tool = rt.registry.get("start_study_session")
    text = await tool.invoke(
        {"minutes": 50, "topic": "Active Directory"}, rt.tool_context(chat["id"])
    )
    assert text.startswith("Study session started on Active Directory: focus until")
    focus, back = rt.store.list_automations()
    assert focus["kind"] == back["kind"] == "reminder"
    assert focus["conversation_id"] == back["conversation_id"] == chat["id"]
    assert focus["prompt"] == "Focus block on Active Directory done. Take a 10-minute break."
    assert back["prompt"] == "Break over. Back to Active Directory."
    assert 590 <= back["next_run"] - focus["next_run"] <= 610
    with pytest.raises(Exception, match="5 to 180 minutes"):
        await tool.invoke({"minutes": 500}, rt.tool_context(chat["id"]))


def test_study_tools_load_in_study_mode(make_runtime):
    rt = make_runtime()
    tools = list(rt.registry.tools.values())

    def offered(text, force=()):
        return {t.name for t in select_tools(tools, [{"role": "user", "content": text}], force)}

    assert "add_flashcards" in offered("Quiz me on subnetting")
    assert "add_flashcards" not in offered("What's the weather in Porto?")
    assert {"add_flashcards", "start_study_session"} <= offered("hello", ("study", "knowledge"))


# API --------------------------------------------------------------------------------------------


@pytest.fixture
def client(make_runtime):
    rt = make_runtime()
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        yield c


def test_flashcards_api(client):
    body = {
        "deck": "Linux",
        "cards": [{"front": "List files?", "back": "ls"}, {"front": "Who am I?", "back": "whoami"}],
    }
    added = client.post("/api/flashcards", json=body)
    assert added.status_code == 201 and added.json()["added"] == 2
    assert client.post("/api/flashcards", json=body).json()["duplicates"] == [
        "List files?",
        "Who am I?",
    ]
    assert client.post("/api/flashcards", json={"deck": "X", "cards": []}).status_code == 422

    assert client.get("/api/flashcards/decks").json() == [
        {"deck": "Linux", "total": 2, "due": 2, "new": 2}
    ]
    due = client.get("/api/flashcards", params={"deck": "linux", "due": 1}).json()
    assert [c["front"] for c in due] == ["List files?", "Who am I?"] and due[0]["is_due"]

    graded = client.post(f"/api/flashcards/{due[0]['id']}/grade", json={"quality": 4}).json()
    assert graded["interval"] == 1 and graded["reps"] == 1 and graded["is_due"] is False
    assert client.post("/api/flashcards/999/grade", json={"quality": 4}).status_code == 404
    assert (
        client.post(f"/api/flashcards/{due[0]['id']}/grade", json={"quality": 9}).status_code == 422
    )
    assert len(client.get("/api/flashcards", params={"due": "1"}).json()) == 1
    assert len(client.get("/api/flashcards").json()) == 2

    assert client.delete(f"/api/flashcards/{due[1]['id']}").json() == {"ok": True}
    assert client.delete(f"/api/flashcards/{due[1]['id']}").status_code == 404


def test_flashcards_csv_api(client):
    client.post(
        "/api/flashcards",
        json={
            "deck": "Ports",
            "cards": [{"front": "SSH", "back": "22"}, {"front": "DNS, UDP", "back": "53"}],
        },
    )
    export = client.get("/api/flashcards/export", params={"deck": "Ports"})
    assert export.headers["content-type"].startswith("text/csv")
    assert export.headers["content-disposition"] == 'attachment; filename="ports.csv"'
    assert export.text == 'front,back,deck\nSSH,22,Ports\n"DNS, UDP",53,Ports\n'

    imported = client.post(
        "/api/flashcards/import", params={"deck": "Copy"}, content=export.text.encode()
    ).json()
    assert imported == {"added": 2, "duplicates": 0, "skipped": 0, "decks": ["Copy"]}
    decks = {d["deck"]: d["total"] for d in client.get("/api/flashcards/decks").json()}
    assert decks == {"Copy": 2, "Ports": 2}
    too_big = client.post("/api/flashcards/import", content=b"a,b\n" * 600_000)
    assert too_big.status_code == 413


# CLI --------------------------------------------------------------------------------------------


@pytest.fixture
def cards_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BAGLEY_URL", f"http://127.0.0.1:{free_port()}")  # No Bagley running.

    def run(*argv: str, keys: str = "") -> int:
        monkeypatch.setattr("sys.stdin", io.StringIO(keys))
        return cli.main(["cards", *argv])

    run.db = tmp_path / "data" / "bagley.db"
    return run


def test_cards_command(cards_cli, capsys):
    assert cards_cli("list") == 0
    assert "» DECKS // 000" in capsys.readouterr().out
    assert cards_cli("add", "Net", "Port of SSH?", "22") == 0
    assert cards_cli("add", "Net", "Port of DNS?", "53") == 0
    assert cards_cli("add", "Net", "port of ssh?", "22") == 0
    out = capsys.readouterr().out
    assert "[OK] ADDED // Net #1" in out and "[WARN] ALREADY IN Net" in out
    assert cards_cli("add", "Net") == 2

    assert cards_cli("list", "--json") == 0
    assert json.loads(capsys.readouterr().out) == [{"deck": "Net", "total": 2, "due": 2, "new": 2}]

    # Card 1: reveal, "a" (again, 1); card 2: reveal, "g" (good, 4); card 1 again: "5".
    assert cards_cli("Net", keys="\na\n\nx\ng\n\n5\n") == 0
    out = capsys.readouterr().out
    assert out.startswith("» REVIEW // NET  002 DUE")
    assert "Q  Port of SSH?" in out and "A  22" in out
    assert "[AGAIN] NEXT" in out and "[ERR]" in out and "[OK] NEXT" in out
    assert "[003/003]" in out
    assert "» SESSION END  003 REVIEWED  002 OK  001 AGAIN" in out
    store = Store(cards_cli.db)
    try:
        cards = {c["front"]: c for c in study.list_cards(store)}
        assert cards["Port of SSH?"]["lapses"] == 1 and cards["Port of SSH?"]["reps"] == 1
        assert cards["Port of DNS?"]["reps"] == 1
    finally:
        store.close()

    assert cards_cli(keys="") == 0
    assert "NOTHING DUE" in capsys.readouterr().out
