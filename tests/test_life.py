from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from bagley import cli, life
from bagley.commands.life import Ctos, render
from bagley.policy import READS_PRIVATE
from bagley.server import create_app
from bagley.store import Store
from bagley.toolroute import select_tools
from bagley.tools import ToolError
from tests.demo_stack import free_port, serve_in_thread
from tests.mock_llm import MockLLM, Reply

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 8, 15, 0).astimezone()  # A Thursday.
TUESDAY = life.parse_period("tuesday", NOW)


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone()


def ts(text: str) -> float:
    return local(text).timestamp()


def days(period: life.Period) -> tuple[str, str, str]:
    return (f"{period.start:%Y-%m-%d}", f"{period.end:%Y-%m-%d}", period.label)


# Periods ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today", ("2026-10-08", "2026-10-09", "today")),
        ("Yesterday", ("2026-10-07", "2026-10-08", "yesterday")),
        ("tuesday", ("2026-10-06", "2026-10-07", "tuesday")),
        ("on Tue?", ("2026-10-06", "2026-10-07", "tuesday")),
        ("last tuesday", ("2026-10-06", "2026-10-07", "last tuesday")),
        ("thursday", ("2026-10-08", "2026-10-09", "thursday")),
        ("last thursday", ("2026-10-01", "2026-10-02", "last thursday")),
        ("friday", ("2026-10-02", "2026-10-03", "friday")),
        ("this week", ("2026-10-05", "2026-10-12", "this week")),
        ("last week", ("2026-09-28", "2026-10-05", "last week")),
        ("past 7 days", ("2026-10-02", "2026-10-09", "past 7 days")),
        ("the past week", ("2026-10-02", "2026-10-09", "past 7 days")),
        ("last 2 weeks", ("2026-09-25", "2026-10-09", "past 14 days")),
        ("3 days ago", ("2026-10-05", "2026-10-06", "3 days ago")),
        ("2026-10-06", ("2026-10-06", "2026-10-07", "tuesday 6 oct")),
        ("6 oct", ("2026-10-06", "2026-10-07", "tuesday 6 oct")),
        ("October 6", ("2026-10-06", "2026-10-07", "tuesday 6 oct")),
        ("6th of October, 2025", ("2025-10-06", "2025-10-07", "monday 6 oct")),
        ("dec 25", ("2025-12-25", "2025-12-26", "thursday 25 dec")),
        ("last month", ("2026-09-01", "2026-10-01", "last month")),
        ("this month", ("2026-10-01", "2026-11-01", "this month")),
        ("september", ("2026-09-01", "2026-10-01", "september 2026")),
    ],
)
def test_parse_period(text, expected):
    assert days(life.parse_period(text, NOW)) == expected


def test_weekday_names_on_that_weekday():
    tuesday = datetime(2026, 10, 6, 9, 0).astimezone()
    assert days(life.parse_period("tuesday", tuesday))[:2] == ("2026-10-06", "2026-10-07")
    assert days(life.parse_period("last tuesday", tuesday))[:2] == ("2026-09-29", "2026-09-30")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "Say which day"),
        ("someday", "Couldn't read 'someday'"),
        ("next week", "Couldn't read"),
        ("2026-13-01", "not a valid date"),
        ("31 feb", "not a valid date"),
        ("2026-12-01", "in the future"),
        ("past 900 days", "1 to 366 days"),
    ],
)
def test_bad_periods(text, message):
    with pytest.raises(life.PeriodError, match=message):
        life.parse_period(text, NOW)


def test_calendar_weeks():
    assert days(life.week(0, NOW)) == ("2026-10-05", "2026-10-12", "this week")
    assert days(life.week(1, NOW)) == ("2026-09-28", "2026-10-05", "last week")
    assert days(life.week(2, NOW)) == ("2026-09-21", "2026-09-28", "week of monday 21 sep")


# Git ----------------------------------------------------------------------------------------------

LOG = (
    "aaaaaaaaaaaa\x1fMe\x1fme@example.com\x1f{t1}\x1fAdd flashcards table\n"
    "10\t2\tbagley/study.py\n"
    "-\t-\tdocs/shot.png\n"
    "\n"
    "bbbbbbbbbbbb\x1fSam\x1fsam@example.com\x1f{t1}\x1fSomeone else's work\n"
    "1\t1\tREADME.md\n"
    "\n"
    "cccccccccccc\x1fMe\x1fME@example.com\x1f{t2}\x1fWIP: recap kind\n"
    "30\t0\tbagley/life.py\n"
    "5\t5\tbagley/study.py\n"
)


def fake_git(calls: list[list[str]] | None = None, email: str = "me@example.com"):
    t1, t2 = int(ts("2026-10-06T10:15")), int(ts("2026-10-06T16:40"))

    def run(args: list[str]) -> str:
        if calls is not None:
            calls.append(args)
        if args[3:5] == ["config", "user.email"]:
            if not email:
                raise life.CommandError("exit 1")
            return email + "\n"
        if args[3] == "log":
            return LOG.format(t1=t1, t2=t2)
        if args[3] == "for-each-ref":
            return (
                f"main\t{t2}\t<me@example.com>\t<me@example.com>\n"
                f"feature/recap\t{t2}\t<me@example.com>\t<me@example.com>\n"
                f"old\t{int(ts('2026-09-01T10:00'))}\t<me@example.com>\t<me@example.com>\n"
                f"theirs\t{t1}\t<sam@example.com>\t<sam@example.com>\n"
            )
        raise AssertionError(args)

    return run


@pytest.fixture
def code(tmp_path):
    """A code folder with repositories at several depths, and some that must be skipped."""
    root = tmp_path / "dev"
    for rel in [
        "proj",
        "group/sub/deep",
        "group/sub/deep/inner",
        "a/b/c/too-deep",
        "node_modules/pkg",
        "web/node_modules/pkg",
        ".hidden/repo",
    ]:
        (root / rel / ".git").mkdir(parents=True)
    (root / "notes").mkdir()
    return root


def test_find_repos(code):
    found = {p.relative_to(code).as_posix() for p in life.find_repos([code, code / "missing"])}
    assert found == {"proj", "group/sub/deep"}


def test_parse_git_log():
    commits = life.parse_git_log(LOG.format(t1=1, t2=2))
    assert [c["subject"] for c in commits] == [
        "Add flashcards table",
        "Someone else's work",
        "WIP: recap kind",
    ]
    assert commits[0]["files"] == 2 and commits[0]["added"] == 10 and commits[0]["removed"] == 2
    assert commits[2]["email"] == "me@example.com"


def test_collect_git_keeps_the_users_commits(code):
    calls: list[list[str]] = []
    repos, warnings = life.collect_git([str(code)], *TUESDAY[:2], runner=fake_git(calls))
    assert warnings == []
    # Both repositories report the same commits (like two worktrees): they count once.
    (repo,) = repos
    assert repo["name"] == "deep"
    assert [c["subject"] for c in repo["commits"]] == ["Add flashcards table", "WIP: recap kind"]
    assert repo["branches"] == ["main", "feature/recap"]
    logs = [c for c in calls if c[3] == "log"]
    assert [c[1:3] for c in logs] == [
        ["-C", str(code / "group/sub/deep")],
        ["-C", str(code / "proj")],
    ]
    assert "--all" in logs[0] and "--no-merges" in logs[0] and "--numstat" in logs[0]
    assert f"--since={TUESDAY.start.isoformat()}" in logs[0]
    assert "--pretty=format:%H%x1f%an%x1f%ae%x1f%at%x1f%s" in logs[0]

    everyone, _ = life.collect_git([str(code)], *TUESDAY[:2], runner=fake_git(), emails=["*"])
    assert len(everyone[0]["commits"]) == 3 and "theirs" in everyone[0]["branches"]

    nobody, warnings = life.collect_git([str(code)], *TUESDAY[:2], runner=fake_git(email=""))
    assert nobody == [] and "no git user.email" in warnings[0]
    _, warnings = life.collect_git([str(code / "nope")], *TUESDAY[:2], runner=fake_git())
    assert warnings == [f"Code folder {code / 'nope'} does not exist."]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_real_git_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "dev" / "proj"
    repo.mkdir(parents=True)

    def git(*args: str, when: str = "", committed: str = "", email: str = "me@example.com"):
        env = {**os.environ}
        if when:
            env["GIT_AUTHOR_DATE"] = local(when).isoformat()
            env["GIT_COMMITTER_DATE"] = local(committed or when).isoformat()
        who = ["-c", f"user.email={email}", "-c", "user.name=Someone"]
        subprocess.run(
            ["git", "-C", str(repo), *who, *args], check=True, env=env, capture_output=True
        )

    git("init", "-q")
    git("checkout", "-q", "-b", "main")
    git("config", "user.email", "me@example.com")
    for name, when, email in [
        ("a.txt", "2026-10-05T18:00", "me@example.com"),  # Monday.
        ("b.txt", "2026-10-06T09:30", "me@example.com"),
        ("c.txt", "2026-10-06T11:00", "sam@example.com"),
    ]:
        (repo / name).write_text("one\ntwo\n")
        git("add", name)
        git("commit", "-q", "-m", f"Add {name}", when=when, email=email)
    git("checkout", "-q", "-b", "feature/x")
    (repo / "b.txt").write_text("one\n")
    git("commit", "-q", "-am", "Trim b", when="2026-10-06T15:45")
    git("checkout", "-q", "-b", "fix", "main")
    (repo / "a.txt").write_text("one\n")
    # Written on Tuesday, rebased on Thursday: it still belongs to Tuesday.
    git("commit", "-q", "-am", "Fix a", when="2026-10-06T17:00", committed="2026-10-08T09:00")

    repos, warnings = life.collect_git([str(tmp_path / "dev")], *TUESDAY[:2])
    assert warnings == []
    assert [(c["subject"], c["added"], c["removed"]) for c in repos[0]["commits"]] == [
        ("Add b.txt", 2, 0),
        ("Trim b", 0, 1),
        ("Fix a", 0, 1),
    ]
    assert repos[0]["branches"] == ["feature/x"]


# Obsidian ---------------------------------------------------------------------------------------


def write(path, text: str, mtime: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (ts(mtime), ts(mtime)))


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "Brain"
    write(
        root / "Daily" / "2026-10-06.md",
        "---\ntags: [daily]\n---\n# Tuesday\n\nSubnetting lab with #networking, see [[VLSM]].\n\n"
        "- [ ] finish lab 3\n- [x] read chapter 2\n",
        "2026-10-07T08:00",  # Edited the next morning.
    )
    write(
        root / "Projects" / "Lab.md",
        "---\ncreated: 2026-10-06T09:15\ntags:\n  - lab\n  - cisco\n---\nRouter config [[OSPF|areas]].",
        "2026-10-08T10:00",
    )
    write(
        root / "Edited.md",
        "Changed today. #study\n\n```\n#notatag\n```\n[link](#anchor) #2026 issue#1",
        "2026-10-06T16:00",
    )
    write(root / "Old.md", "Untouched.", "2026-09-01T10:00")
    write(root / "Daily" / "2026-10-05.md", "Monday.", "2026-10-05T20:00")
    write(root / ".obsidian" / "workspace.md", "settings", "2026-10-06T12:00")
    write(root / ".trash" / "gone.md", "deleted", "2026-10-06T12:00")
    return root


def test_frontmatter_and_tags():
    meta, body = life.frontmatter('---\ntitle: "X"\ntags: a, b\naliases: [one, two]\n---\nBody #c')
    assert meta == {"title": "X", "tags": "a, b", "aliases": ["one", "two"]}
    assert body == "Body #c"
    assert life.note_tags(meta, body) == ["a", "b", "c"]
    assert life.frontmatter("No front matter")[0] == {}
    assert life.parse_when("2026-10-06") == ts("2026-10-06T00:00")
    assert life.parse_when("2026-10-06 14:30") == ts("2026-10-06T14:30")
    assert (
        life.parse_when("2026-10-06T12:30:00Z")
        == datetime.fromisoformat("2026-10-06T12:30:00+00:00").timestamp()
    )
    assert life.parse_when("someday") is None


def test_vault_activity(vault):
    notes, daily = life.vault_activity(vault, *TUESDAY[:2])
    by_path = {n["path"]: n for n in notes}
    assert set(by_path) == {"Daily/2026-10-06.md", "Projects/Lab.md", "Edited.md"}
    assert by_path["Projects/Lab.md"]["status"] == "created"
    assert by_path["Projects/Lab.md"]["ts"] == ts("2026-10-06T09:15")
    assert by_path["Projects/Lab.md"]["tags"] == ["lab", "cisco"]
    assert by_path["Projects/Lab.md"]["links"] == ["OSPF"]
    assert by_path["Edited.md"]["status"] == "modified"
    assert by_path["Edited.md"]["tags"] == ["study"]
    assert by_path["Daily/2026-10-06.md"]["status"] == "daily"
    assert by_path["Daily/2026-10-06.md"]["tags"] == ["daily", "networking"]
    assert daily == [
        {
            "date": "2026-10-06",
            "vault": "Brain",
            "path": "Daily/2026-10-06.md",
            "text": "# Tuesday Subnetting lab with #networking, see [[VLSM]]. - [x] read chapter 2",
            "open_tasks": ["finish lab 3"],
        }
    ]


# Chats, automations and the whole picture -------------------------------------------------------


def chat_at(rt, title: str, when: str, mode: str = "default", n: int = 2) -> str:
    conv = rt.store.create_conversation(title, mode=mode)
    for i in range(n):
        rt.store.add_message(conv["id"], "user" if i % 2 == 0 else "assistant", f"m{i}")
    rt.store.execute(
        "UPDATE messages SET created_at = ? WHERE conversation_id = ?", (ts(when), conv["id"])
    )
    return conv["id"]


async def test_activity_combines_every_source(make_runtime, tmp_path, vault):
    code = tmp_path / "code"
    (code / "proj" / ".git").mkdir(parents=True)
    rt = make_runtime(code_folders=[str(code)], vaults=[str(vault)])
    quiz = chat_at(rt, "Subnetting quiz", "2026-10-06T11:00", mode="study", n=4)
    chat_at(rt, "Wednesday chat", "2026-10-07T11:00")
    gone = chat_at(rt, "Deleted", "2026-10-06T12:00")
    rt.store.delete_conversation(gone)
    reminder = rt.scheduler.create("reminder", "Stretch", "in 5 minutes", prompt="Stretch")
    rt.store.update_automation(reminder["id"], last_run=ts("2026-10-06T15:00"), last_status="ok")

    data = await life.activity(rt, *TUESDAY[:2], label="tuesday", runner=fake_git())
    assert data["period"]["label"] == "tuesday" and data["period"]["days"] == 1
    assert data["totals"] == {
        "commits": 2,
        "repos": 1,
        "added": 45,
        "removed": 7,
        "notes": 3,
        "daily_notes": 1,
        "chats": 1,
        "automations": 1,
    }
    (day,) = data["days"]
    assert day["date"] == "2026-10-06" and day["day"] == "tue"
    assert day["commits"] == 2 and day["notes"] == 3 and day["chats"] == 1
    assert day["subjects"] == ["Add flashcards table", "WIP: recap kind"]
    (repo,) = data["repos"]
    assert repo["path"].endswith("proj") and repo["files"] == 3
    assert repo["top_paths"][0] == "bagley/life.py"
    assert repo["log"][0] == {
        "time": "2026-10-06T10:15",
        "subject": "Add flashcards table",
        "files": 2,
        "added": 10,
        "removed": 2,
    }
    assert [n["path"] for n in data["notes"]] == [
        "Daily/2026-10-06.md",
        "Projects/Lab.md",
        "Edited.md",
    ]
    assert data["daily_notes"][0]["open_tasks"] == ["finish lab 3"]
    assert data["tags"]["networking"] == 1
    assert data["chats"] == [
        {
            "id": quiz,
            "time": "2026-10-06T11:00",
            "title": "Subnetting quiz",
            "mode": "study",
            "messages": 4,
        }
    ]
    assert data["automations"] == [
        {
            "id": reminder["id"],
            "time": "2026-10-06T15:00",
            "kind": "reminder",
            "name": "Stretch",
            "status": "ok",
        }
    ]
    assert data["warnings"] == []
    assert life.open_threads(data) == [
        "proj: WIP: recap kind",
        "proj: branches feature/recap",
        "2026-10-06: finish lab 3",
    ]

    small = await life.activity(rt, *TUESDAY[:2], budget=600, runner=fake_git())
    assert len(json.dumps(small)) < len(json.dumps(data))
    assert small["repos"][0]["log"] == [] and small["repos"][0]["more"] == 2
    assert small["totals"] == data["totals"]  # Counts survive trimming.

    draft = life.render_markdown(data)
    assert "2 commits in 1 repositories" in draft and "Projects/Lab.md (created)" in draft


async def test_activity_reports_missing_sources(make_runtime, tmp_path):
    rt = make_runtime(code_folders=[str(tmp_path / "nowhere")])
    data = await life.activity(rt, *TUESDAY[:2], runner=fake_git())
    assert data["totals"]["commits"] == 0
    assert data["warnings"] == [
        f"Code folder {tmp_path / 'nowhere'} does not exist.",
        "No Obsidian vaults are set up (preference 'vaults').",
    ]


async def test_vaults_are_searchable(make_runtime, vault):
    rt = make_runtime(vaults=[str(vault)], embedding_model="off")
    assert vault.resolve() in rt.knowledge.folders()
    await rt.knowledge.reindex()
    hits = await rt.knowledge.search("router config")
    assert hits and hits[0]["path"] == "Brain/Projects/Lab.md"
    assert await rt.knowledge.search("settings") == []  # .obsidian is skipped.


# Tools --------------------------------------------------------------------------------------------


async def test_tools(make_runtime, vault, tmp_path):
    rt = make_runtime(code_folders=[str(tmp_path / "empty")], vaults=[str(vault)])
    (tmp_path / "empty").mkdir()
    ctx = rt.tool_context()
    tool = rt.registry.get("what_was_i_doing")
    assert tool.category == "life" and tool.risk == "safe"
    result = json.loads(await tool.invoke({"period": "past 7 days"}, ctx))
    assert result["period"]["label"] == "past 7 days"
    with pytest.raises(ToolError, match="Couldn't read"):
        await tool.invoke({"period": "the other day"}, ctx)

    recap = json.loads(await rt.registry.get("weekly_recap").invoke({"weeks_ago": 1}, ctx))
    assert recap["activity"]["period"]["label"] == "last week"
    assert recap["structure"][0].startswith("Highlights") and "open_threads" in recap
    assert {"what_was_i_doing", "weekly_recap"} <= READS_PRIVATE


def test_life_tools_load_on_demand(make_runtime):
    rt = make_runtime()
    tools = list(rt.registry.tools.values())
    names = {
        t.name
        for t in select_tools(
            tools, [{"role": "user", "content": "What was I working on Tuesday?"}]
        )
    }
    assert "what_was_i_doing" in names
    names = {
        t.name for t in select_tools(tools, [{"role": "user", "content": "Weather in Porto?"}])
    }
    assert "what_was_i_doing" not in names and "load_tools" in names


# The weekly recap automation ---------------------------------------------------------------------


async def test_recap_automation(make_runtime, mock, tmp_path):
    vault = tmp_path / "Vault"
    (vault / "Daily").mkdir(parents=True)
    (vault / "Daily" / "Lab notes.md").write_text("VLAN trunking. #networking")
    rt = make_runtime(code_folders=[str(tmp_path / "dev")], vaults=[str(vault)])
    (tmp_path / "dev").mkdir()
    chat = rt.store.create_conversation("Subnetting quiz", mode="study")
    rt.store.add_message(chat["id"], "user", "quiz me")
    seen = []

    async def listener(event):
        seen.append(event)

    rt.listeners.add(listener)
    item = rt.scheduler.create("recap", "Weekly recap", "fridays at 17:00")
    assert rt.scheduler.describe(item)["schedule_text"] == "Fridays at 17:00"
    rt.store.update_automation(item["id"], next_run=time.time() - 1)
    mock.script = [Reply(text="## Highlights\n- Studied VLAN trunking.")]
    await rt.scheduler.tick()

    done = rt.store.get_automation(item["id"])
    assert done["last_status"] == "ok" and done["enabled"]
    assert done["state"]["totals"]["notes"] == 1 and done["state"]["period"] == "past 7 days"
    messages = rt.store.list_messages(done["conversation_id"])
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant"]
    assert messages[0]["content"].startswith("**WEEKLY RECAP // DRAFT**")
    assert "Daily/Lab notes.md (modified)" in messages[0]["content"]
    assert "<activity>" in messages[1]["content"] and "Subnetting quiz" in messages[1]["content"]
    assert messages[2]["content"] == "## Highlights\n- Studied VLAN trunking."
    request = mock.requests[-1]
    assert not request.get("tools")  # The model writes from the data; it can't act on it.
    notes = [e for e in seen if e["type"] == "notification"]
    assert len(notes) == 1 and notes[0]["title"] == "WEEKLY RECAP"
    assert notes[0]["level"] == "important" and "1 notes" in notes[0]["body"]


# API ------------------------------------------------------------------------------------------------


@pytest.fixture
def client(make_runtime, code, vault):
    rt = make_runtime(code_folders=[str(code)], vaults=[str(vault), str(vault.parent / "Gone")])
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        c.runtime = rt
        yield c


def test_api(client, code, vault):
    body = client.get("/api/life/activity", params={"period": "past 7 days"}).json()
    assert body["period"]["label"] == "past 7 days" and "totals" in body and "days" in body
    bad = client.get("/api/life/activity", params={"period": "someday"})
    assert bad.status_code == 422 and "Couldn't read" in bad.json()["detail"]

    sources = client.get("/api/life/sources").json()
    assert sources["vaults"] == [
        {"path": str(vault), "exists": True, "notes": 5},
        {"path": str(vault.parent / "Gone"), "exists": False, "notes": 0},
    ]
    assert sorted(r["name"] for r in sources["repos"]) == ["deep", "proj"]

    first = client.post("/api/life/recap-automation").json()
    assert first["created"] is True and first["automation"]["kind"] == "recap"
    assert first["automation"]["name"] == "Weekly recap"
    assert first["automation"]["schedule_text"] == "Fridays at 17:00"
    again = client.post("/api/life/recap-automation").json()
    assert again["created"] is False and again["automation"]["id"] == first["automation"]["id"]


# CLI ------------------------------------------------------------------------------------------------


@pytest.fixture
def recap_cli(tmp_path, monkeypatch):
    mock = MockLLM()
    port = free_port()
    server = serve_in_thread(mock.app, port)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BAGLEY_URL", f"http://127.0.0.1:{free_port()}")  # No Bagley running.
    monkeypatch.setenv("BAGLEY_PROVIDER", "ollama")
    monkeypatch.setenv("BAGLEY_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("BAGLEY_MODEL", "qwen3:8b")
    (tmp_path / "dev").mkdir()
    vault = tmp_path / "Inbox"
    store = Store(tmp_path / "data" / "bagley.db")
    store.set_preferences({"vaults": [str(vault)], "code_folders": [str(tmp_path / "dev")]})
    store.close()
    write(vault / "Today.md", "Written today #cli", datetime.now().isoformat(timespec="minutes"))

    def run(*argv: str) -> int:
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        return cli.main(["recap", *argv])

    run.mock = mock
    yield run
    server.should_exit = True


def test_recap_command(recap_cli, capsys):
    assert recap_cli("today") == 0
    out = capsys.readouterr().out
    assert out.startswith(f"» ACTIVITY // today {datetime.now():%d%m%y}")
    assert "[NOTE]" in out and "Inbox/Today.md" in out and "#cli" in out

    assert recap_cli("today", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["notes"] == 1

    assert recap_cli("someday") == 2
    assert "Couldn't read" in capsys.readouterr().err


def test_recap_command_writes_with_the_model(recap_cli, capsys):
    recap_cli.mock.script = [Reply(text="Highlights: wrote a note.")]
    assert recap_cli("today", "--write") == 0
    captured = capsys.readouterr()
    assert captured.out == "Highlights: wrote a note.\n"
    assert "» RECAP // today" in captured.err
    request = recap_cli.mock.requests[-1]
    assert not request.get("tools")
    assert "<activity>" in request["messages"][-1]["content"]


def test_render_readout(make_runtime):
    data = life.summarize(
        {
            "start": TUESDAY.start,
            "end": TUESDAY.end,
            "label": "tuesday",
            "repos": [
                {
                    "name": "proj",
                    "path": "~/dev/proj",
                    "branches": ["main"],
                    "commits": life.parse_git_log(
                        LOG.format(t1=int(ts("2026-10-06T10:15")), t2=int(ts("2026-10-06T16:40")))
                    ),
                }
            ],
            "notes": [],
            "daily": [],
            "chats": [
                {
                    "id": "x",
                    "ts": ts("2026-10-06T11:00"),
                    "last": 0,
                    "title": "Quiz",
                    "mode": "study",
                    "messages": 6,
                    "automation": False,
                }
            ],
            "automations": [],
            "sources": {},
            "warnings": ["Vault ~/Gone does not exist."],
        }
    )
    lines = render(data, Ctos(io.StringIO()))
    assert lines[0] == "» ACTIVITY // tuesday 061026"
    assert "COMMITS 003" in lines[1] and "CHATS 001" in lines[1]
    text = "\n".join(lines)
    assert "[GIT] proj ~/dev/proj  003 COMMITS  +46 -8" in text
    assert "-1015- Add flashcards table  02F +10 -2" in text
    assert "[CHAT] -1100- Quiz  STDY  006 MSG" in text
    assert "[WARN] Vault ~/Gone does not exist." in text
