from __future__ import annotations

import sqlite3
import sys

import httpx
import pytest

from bagley import journal
from bagley.store import Store
from bagley.tools import ToolContext, ToolError, build_registry

pytestmark = pytest.mark.anyio


@pytest.fixture
def ctx(config):
    config.ensure_dirs()
    store = Store(config.db_path)
    yield ToolContext(config=config, store=store, http=httpx.AsyncClient(), conversation_id="c1")
    store.close()


async def call(ctx, name, **args):
    return await build_registry(ctx.config).get(name).run(args, ctx)


async def test_write_shows_diff_and_reverts(ctx):
    ws = ctx.config.workspace
    (ws / "a.txt").write_text("one\ntwo\n")
    text, ui = await call(ctx, "write_file", path="a.txt", content="one\nTWO\n")
    assert text.startswith("Overwrote a.txt")
    assert "-two" in ui["diff"] and "+TWO" in ui["diff"]
    journal.revert(ctx.store, ctx.config, ui["journal_id"])
    assert (ws / "a.txt").read_text() == "one\ntwo\n"
    with pytest.raises(journal.JournalError, match="already reverted"):
        journal.revert(ctx.store, ctx.config, ui["journal_id"])


async def test_reverting_a_created_file_removes_it(ctx):
    _, ui = await call(ctx, "write_file", path="new/b.md", content="hi")
    journal.revert(ctx.store, ctx.config, ui["journal_id"])
    assert not (ctx.config.workspace / "new" / "b.md").exists()


async def test_revert_refuses_when_file_changed_since(ctx):
    _, ui = await call(ctx, "write_file", path="c.txt", content="bagley")
    (ctx.config.workspace / "c.txt").write_text("user edit")
    with pytest.raises(journal.JournalError, match="changed since"):
        journal.revert(ctx.store, ctx.config, ui["journal_id"])
    assert (ctx.config.workspace / "c.txt").read_text() == "user edit"


async def test_edit_file(ctx):
    (ctx.config.workspace / "d.txt").write_text("alpha beta alpha")
    with pytest.raises(ToolError, match="2 times"):
        await call(ctx, "edit_file", path="d.txt", find="alpha", replace="x")
    with pytest.raises(ToolError, match="not found"):
        await call(ctx, "edit_file", path="d.txt", find="gamma", replace="x")
    _, ui = await call(ctx, "edit_file", path="d.txt", find="beta", replace="BETA")
    assert (ctx.config.workspace / "d.txt").read_text() == "alpha BETA alpha"
    journal.revert(ctx.store, ctx.config, ui["journal_id"])
    assert (ctx.config.workspace / "d.txt").read_text() == "alpha beta alpha"


async def test_move_delete_and_mkdir_revert(ctx):
    ws = ctx.config.workspace
    (ws / "notes").mkdir()
    (ws / "notes" / "e.txt").write_text("keep me")

    _, moved = await call(ctx, "move_file", source="notes/e.txt", destination="archive/e.txt")
    assert (ws / "archive" / "e.txt").exists()
    journal.revert(ctx.store, ctx.config, moved["journal_id"])
    assert (ws / "notes" / "e.txt").read_text() == "keep me"

    _, deleted = await call(ctx, "delete_file", path="notes")
    assert not (ws / "notes").exists()
    journal.revert(ctx.store, ctx.config, deleted["journal_id"])
    assert (ws / "notes" / "e.txt").read_text() == "keep me"

    _, made = await call(ctx, "make_directory", path="empty/inner")
    journal.revert(ctx.store, ctx.config, made["journal_id"])
    assert not (ws / "empty" / "inner").exists()

    with pytest.raises(ToolError, match="whole workspace"):
        await call(ctx, "delete_file", path=".")


def test_schema_migration_adds_unread(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL, "
        "updated_at REAL NOT NULL, deleted_at REAL)"
    )
    db.execute("INSERT INTO conversations VALUES ('x', 'Old chat', 1, 1, NULL)")
    db.commit()
    db.close()
    store = Store(path)
    assert store.list_conversations()[0] == {
        "id": "x",
        "title": "Old chat",
        "created_at": 1,
        "updated_at": 1,
        "unread": 0,
    }
    store.close()


async def test_edit_keeps_windows_line_endings(ctx):
    path = ctx.config.workspace / "crlf.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    await call(ctx, "edit_file", path="crlf.txt", find="two\nthree", replace="2\n3")
    assert path.read_bytes() == b"one\r\n2\r\n3\r\n"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
async def test_delete_and_move_act_on_symlinks_not_their_targets(ctx):
    ws = ctx.config.workspace
    (ws / "data").mkdir()
    (ws / "data" / "real.csv").write_text("a,b")
    (ws / "latest.csv").symlink_to(ws / "data" / "real.csv")
    _, ui = await call(ctx, "delete_file", path="latest.csv")
    assert not (ws / "latest.csv").is_symlink() and (ws / "data" / "real.csv").exists()
    journal.revert(ctx.store, ctx.config, ui["journal_id"])
    assert (ws / "latest.csv").is_symlink()
    await call(ctx, "move_file", source="latest.csv", destination="current.csv")
    assert (ws / "current.csv").is_symlink() and (ws / "data" / "real.csv").exists()
