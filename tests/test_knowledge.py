from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bagley.knowledge import chunk, is_embedding_model, read_text
from bagley.server import create_app

pytestmark = pytest.mark.anyio


def test_chunk_tracks_headings_and_size():
    text = (
        "# Plan\n\nIntro.\n\n## Budget\n\n"
        + ("Line about costs. " * 120)
        + "\n\n## Team\n\nAlice and Bob."
    )
    passages = chunk(text, "notes")
    assert passages[0].heading == "notes › Plan"
    assert any(p.heading == "notes › Budget" for p in passages)
    assert passages[-1].heading == "notes › Team" and "Alice" in passages[-1].body
    assert all(len(p.body) <= 1400 for p in passages)


def test_read_text(tmp_path):
    (tmp_path / "a.html").write_text("<html><title>T</title><body><p>Hello</p></body></html>")
    (tmp_path / "b.bin").write_bytes(b"\0\1\2")
    (tmp_path / "c.png").write_bytes(b"x")
    (tmp_path / "d.md").write_bytes(b"\0binary pretending")
    assert read_text(tmp_path / "a.html") == "# T\n\nHello"
    assert read_text(tmp_path / "b.bin") == ""
    assert read_text(tmp_path / "c.png") == ""
    assert read_text(tmp_path / "d.md") == ""
    assert is_embedding_model("nomic-embed-text:latest") and not is_embedding_model("qwen3:8b")


@pytest.fixture
def notes(tmp_path):
    folder = tmp_path / "Notes"
    (folder / "work").mkdir(parents=True)
    (folder / "work" / "q3.md").write_text(
        "# Q3 plan\n\nThe marketing budget is 40k. Hiring two engineers."
    )
    (folder / "travel.md").write_text("# Lisbon trip\n\nFlights booked for March. Hotel in Alfama.")
    (folder / ".hidden").mkdir()
    (folder / ".hidden" / "secret.md").write_text("budget secret")
    return folder


async def test_index_search_update_and_delete(make_runtime, notes):
    rt = make_runtime(knowledge_folders=[str(notes)], embedding_model="off")
    counts = await rt.knowledge.reindex()
    assert counts == {"added": 2, "updated": 0, "removed": 0}
    status = rt.knowledge.status()
    assert status["files"] == 2 and status["embedding_model"] is None
    assert [f["label"] for f in status["folders"]] == ["workspace", "Notes"]

    hits = await rt.knowledge.search("marketing budget")
    assert hits[0]["path"] == "Notes/work/q3.md" and hits[0]["match"] == "keyword"
    assert await rt.knowledge.search("money") == []  # Keyword search alone can't find synonyms.

    (notes / "travel.md").write_text("# Lisbon trip\n\nFlights moved to April.")
    (notes / "work" / "q3.md").unlink()
    assert await rt.knowledge.reindex() == {"added": 0, "updated": 1, "removed": 1}
    assert (await rt.knowledge.search("april flights"))[0]["path"] == "Notes/travel.md"
    assert await rt.knowledge.search("marketing") == []


async def test_semantic_search_with_local_embeddings(make_runtime, mock, notes):
    mock.models["nomic-embed-text:latest"] = {
        "capabilities": ["embedding"],
        "size": 274_000_000,
        "params": "137M",
        "family": "nomic-bert",
    }
    rt = make_runtime(knowledge_folders=[str(notes)])
    await rt.knowledge.reindex()
    status = rt.knowledge.status()
    assert (
        status["embedding_model"] == "nomic-embed-text:latest"
        and status["embedded"] == status["passages"]
    )
    hits = await rt.knowledge.search("how much money can we spend")
    assert hits[0]["path"] == "Notes/work/q3.md" and hits[0]["match"] == "meaning"
    calls = mock.embed_calls
    await rt.knowledge.reindex()
    assert mock.embed_calls == calls  # Unchanged files are not embedded again.
    assert (
        await rt.resolve_model(
            await rt.provider(), rt.preferences()[0].model_copy(update={"model": ""})
        )
        != "nomic-embed-text:latest"
    )


async def test_tools_and_prompt(make_runtime, notes, recorder, mock):
    from bagley.agent import Agent, RunRequest
    from tests.mock_llm import Reply

    rt = make_runtime(knowledge_folders=[str(notes)], embedding_model="off")
    await rt.knowledge.reindex()
    ctx = rt.tool_context()
    found = await rt.registry.get("search_knowledge").invoke({"query": "hotel alfama"}, ctx)
    assert "Notes/travel.md" in found
    doc = await rt.registry.get("read_document").invoke({"path": "Notes/travel.md"}, ctx)
    assert "Flights booked" in doc
    with pytest.raises(Exception, match="not in the knowledge base"):
        await rt.registry.get("read_document").invoke({"path": "../../etc/passwd"}, ctx)

    mock.script = [Reply(text="ok")]
    await Agent(rt).run(RunRequest(text="what's in my notes?"), recorder.emit, recorder.approve)
    system = mock.requests[-1]["messages"][0]["content"]
    assert "private knowledge base of 2 documents (Notes)" in system


def test_knowledge_api(make_runtime, notes, tmp_path):
    rt = make_runtime(embedding_model="off")
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        assert c.post("/api/knowledge/folders", json={"path": "relative/path"}).status_code == 422
        assert (
            c.post("/api/knowledge/folders", json={"path": str(tmp_path / "missing")}).status_code
            == 422
        )
        added = c.post("/api/knowledge/folders", json={"path": str(notes)}).json()
        assert [f["label"] for f in added["folders"]] == ["workspace", "Notes"]
        for _ in range(100):
            if c.get("/api/knowledge").json()["files"] == 2:
                break
            __import__("time").sleep(0.05)
        assert (
            c.get("/api/knowledge/search", params={"q": "alfama"}).json()[0]["path"]
            == "Notes/travel.md"
        )
        assert c.delete("/api/knowledge/folders", params={"path": str(notes)}).json()["files"] == 0
        assert c.delete("/api/knowledge/folders", params={"path": str(notes)}).status_code == 404
