"""The user's own activity by date: git commits, Obsidian notes, Bagley chats and automations.

``parse_period`` reads phrases like "tuesday", "last week" or "6 oct" into a time range. The
collectors read local sources only: git repositories under ``Preferences.code_folders``, the
Obsidian vaults in ``Preferences.vaults`` and Bagley's own store. ``activity`` combines them into
one summary with per-day buckets, trimmed to fit in a model's context. Nothing leaves the machine.

A commit counts as the user's when its author email matches the repository's
``git config user.email`` (which falls back to the global one) or an address listed in
``BAGLEY_GIT_EMAILS`` (comma-separated; ``*`` counts every author).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from bagley.runtime import Runtime
    from bagley.store import Store

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]  # fmt: skip
PERIOD_HELP = (
    "Try 'today', 'yesterday', 'tuesday', 'last tuesday', 'this week', 'last week', "
    "'past 7 days', '2026-10-06', '6 oct', 'october 6' or 'last month'."
)

SKIP_DIRS = {"node_modules", ".venv", "venv", "target", "__pycache__", "dist", "build"}
MAX_REPOS = 200
MAX_LOG = 1000  # Commits read per repository and period.
REBASE_SLACK_DAYS = 14
MAX_OUTPUT = 4_000_000  # Bytes of output kept from one command.
MAX_VAULT_FILES = 50_000
MAX_NOTE_BYTES = 256_000
LOG_FORMAT = "--pretty=format:%H%x1f%an%x1f%ae%x1f%at%x1f%s"
REF_FORMAT = (
    "--format=%(refname:short)%09%(committerdate:unix)%09%(authoremail)%09%(committeremail)"
)
DAILY_NOTE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
NUMSTAT = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
TAG = re.compile(r"(?<![\w#/&(])#([^\W\d][\w/-]*)")
WIKILINK = re.compile(r"\[\[([^\]|#^\n]+)")
OPEN_TASK = re.compile(r"^\s*[-*] \[ \] (.+)$", re.M)
WIP = re.compile(r"\b(wip|todo|fixme|tmp|temp|draft|half)\b", re.I)
MAIN_BRANCHES = {"main", "master", "trunk", "develop", "dev"}

# How much of each list ``activity`` keeps. Later levels apply when the result has to fit a
# character budget (tool results for small models).
LIMITS = {"repos": 10, "log": 10, "paths": 5, "notes": 30, "links": 8, "note_tags": 8, "tags": 20,
          "daily": 10, "excerpt": 300, "tasks": 5, "chats": 20, "automations": 15, "subjects": 3}  # fmt: skip
SHRINK = [
    {"log": 5, "notes": 15, "excerpt": 200, "chats": 12},
    {"log": 3, "notes": 10, "excerpt": 120, "chats": 8, "links": 0, "tags": 10, "daily": 7,
     "tasks": 2},
    {"log": 1, "notes": 5, "excerpt": 60, "chats": 5, "links": 0, "note_tags": 3, "tags": 5,
     "daily": 4, "tasks": 1, "subjects": 1, "repos": 6, "paths": 0, "automations": 5},
    {"log": 0, "notes": 0, "excerpt": 0, "chats": 0, "links": 0, "note_tags": 0, "tags": 0,
     "daily": 0, "tasks": 0, "subjects": 0, "repos": 4, "paths": 0, "automations": 0},
]  # fmt: skip

RECAP_STRUCTURE = [
    "Highlights: 3-5 bullets with the most significant things done",
    "By project: a short paragraph per repository or topic saying what changed",
    "Notes written: the notes created or edited, grouped by theme",
    "Open threads: unfinished work to pick up (WIP commits, open tasks, side branches)",
]


class PeriodError(ValueError):
    pass


class CommandError(Exception):
    pass


class Period(NamedTuple):
    start: datetime
    end: datetime  # Exclusive.
    label: str


# Periods ----------------------------------------------------------------------------------------


def _midnight(day: date) -> datetime:
    return datetime.combine(day, dt_time()).astimezone()


def _days(first: date, count: int, label: str) -> Period:
    return Period(_midnight(first), _midnight(first + timedelta(days=count)), label)


def _prefix(word: str, names: list[str]) -> int | None:
    """Index of the name ``word`` abbreviates ("tue", "tues", "sept"), at least 3 letters."""
    word = word.rstrip(".")
    if len(word) < 3:
        return None
    return next((i for i, name in enumerate(names) if name.startswith(word)), None)


def _date_label(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1][:3]}"


def _past_date(today: date, year: int | None, month: int, day: int, text: str) -> date:
    try:
        found = date(year or today.year, month, day)
        if year is None and found > today:
            found = date(today.year - 1, month, day)  # "6 oct" in January means last October.
    except ValueError as exc:
        raise PeriodError(f"'{text}' is not a valid date.") from exc
    if found > today:
        raise PeriodError(f"{found.isoformat()} is in the future.")
    return found


def week(weeks_ago: int = 0, now: datetime | None = None) -> Period:
    """A calendar week, Monday to Sunday: this week (0), last week (1)..."""
    today = (now or datetime.now()).astimezone().date()
    monday = today - timedelta(days=today.weekday() + 7 * max(0, weeks_ago))
    label = {0: "this week", 1: "last week"}.get(weeks_ago, f"week of {_date_label(monday)}")
    return _days(monday, 7, label)


def parse_period(text: str, now: datetime | None = None) -> Period:
    """Read a day or range in the user's words. Weekday names mean the most recent one, today
    included ("tuesday" on a Tuesday is today); "last tuesday" on a Tuesday is a week ago."""
    today = (now or datetime.now()).astimezone().date()
    s = " ".join(text.lower().replace(",", " ").split()).rstrip("?.!")
    s = re.sub(r"^(?:on|for|during|in|from|since) ", "", s)
    if not s:
        raise PeriodError(f"Say which day or range. {PERIOD_HELP}")

    if s in ("today", "now", "this day"):
        return _days(today, 1, "today")
    if s == "yesterday":
        return _days(today - timedelta(days=1), 1, "yesterday")
    if m := re.fullmatch(r"(\d{1,3}) days? ago", s):
        return _days(today - timedelta(days=int(m.group(1))), 1, s)
    if m := re.fullmatch(r"(?:(last|this) )?([a-z]+\.?)", s):
        idx = _prefix(m.group(2), WEEKDAYS)
        if idx is not None:
            back = (today.weekday() - idx) % 7
            if m.group(1) == "last" and back == 0:
                back = 7
            label = ("last " if m.group(1) == "last" else "") + WEEKDAYS[idx]
            return _days(today - timedelta(days=back), 1, label)

    monday = today - timedelta(days=today.weekday())
    if s in ("this week", "week", "the week"):
        return _days(monday, 7, "this week")
    if s in ("last week", "previous week"):
        return _days(monday - timedelta(days=7), 7, "last week")
    if s in ("past week", "the past week", "last 7 days"):
        s = "past 7 days"
    if m := re.fullmatch(r"(?:the )?(?:past|last) (\d{1,3}) (days?|weeks?)", s):
        count = int(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        if not 1 <= count <= 366:
            raise PeriodError("Pick a range of 1 to 366 days.")
        return _days(today - timedelta(days=count - 1), count, f"past {count} days")

    first = today.replace(day=1)
    if s == "this month":
        nxt = (first + timedelta(days=32)).replace(day=1)
        return Period(_midnight(first), _midnight(nxt), "this month")
    if s in ("last month", "previous month"):
        prev = (first - timedelta(days=1)).replace(day=1)
        return Period(_midnight(prev), _midnight(first), "last month")

    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        day = _past_date(today, int(m.group(1)), int(m.group(2)), int(m.group(3)), text)
        return _days(day, 1, _date_label(day))
    day_month = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]+\.?)(?: (\d{4}))?", s)
    month_day = re.fullmatch(r"([a-z]+\.?) (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", s)
    if day_month or month_day:
        if day_month:
            num, name, year = day_month.groups()
        else:
            assert month_day
            name, num, year = month_day.groups()
        month = _prefix(name, MONTHS)
        if month is not None:
            day = _past_date(today, int(year) if year else None, month + 1, int(num), text)
            return _days(day, 1, _date_label(day))
    if m := re.fullmatch(r"([a-z]+\.?)(?: (\d{4}))?", s):
        month = _prefix(m.group(1), MONTHS)
        if month is not None:
            year = int(m.group(2)) if m.group(2) else today.year
            if not m.group(2) and month + 1 > today.month:
                year -= 1
            start = date(year, month + 1, 1)
            if start > today:
                raise PeriodError(f"{MONTHS[month].title()} {year} is in the future.")
            end = (start + timedelta(days=32)).replace(day=1)
            return Period(_midnight(start), _midnight(end), f"{MONTHS[month]} {year}")
    raise PeriodError(f"Couldn't read '{text.strip()}' as a day or range. {PERIOD_HELP}")


# Running programs -------------------------------------------------------------------------------

Runner = Callable[[list[str]], str]


def run_command(args: list[str], timeout: float = 20.0) -> str:
    """Run a program from an argument list (never a shell) and return its standard output."""
    if not shutil.which(args[0]):
        raise CommandError(f"{args[0]} is not installed.")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(
            args, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{args[0]} took longer than {timeout:g}s.") from exc
    except OSError as exc:
        raise CommandError(str(exc)) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CommandError(detail[0][:300] if detail else f"{args[0]} exited {proc.returncode}.")
    return proc.stdout[:MAX_OUTPUT].decode("utf-8", "replace")


def home(path: Path | str) -> str:
    """A path with the home folder shortened to ~."""
    text, base = str(path), str(Path.home())
    return "~" + text[len(base) :] if text == base or text.startswith(base + os.sep) else text


# Git --------------------------------------------------------------------------------------------


def find_repos(folders: Iterable[str | Path], *, max_depth: int = 3) -> list[Path]:
    """Git repositories under ``folders``, at most ``max_depth`` levels down. A folder with a
    ``.git`` is not searched further; dependency and build folders are skipped."""
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in folders:
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        stack = [(root, 0)]
        while stack and len(found) < MAX_REPOS:
            path, depth = stack.pop()
            if (path / ".git").exists():
                real = path.resolve()
                if real not in seen:
                    seen.add(real)
                    found.append(path)
                continue
            if depth >= max_depth:
                continue
            try:
                children = sorted(
                    p
                    for p in path.iterdir()
                    if p.name not in SKIP_DIRS and not p.name.startswith(".") and p.is_dir()
                )
            except OSError:
                continue
            stack.extend((child, depth + 1) for child in reversed(children))
    return found


def parse_git_log(text: str) -> list[dict[str, Any]]:
    """Commits from ``git log --pretty=format:%H%x1f%an%x1f%ae%x1f%at%x1f%s --numstat``."""
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if "\x1f" in line:
            parts = line.split("\x1f", 4)
            if len(parts) < 5 or not parts[3].strip().isdigit():
                current = None
                continue
            current = {
                "hash": parts[0][:10],
                "author": parts[1],
                "email": parts[2].strip().lower(),
                "ts": float(parts[3]),
                "subject": parts[4].strip(),
                "files": 0,
                "added": 0,
                "removed": 0,
                "paths": {},
            }
            commits.append(current)
        elif current is not None and (m := NUMSTAT.match(line)):
            added = int(m.group(1)) if m.group(1).isdigit() else 0
            removed = int(m.group(2)) if m.group(2).isdigit() else 0
            current["files"] += 1
            current["added"] += added
            current["removed"] += removed
            current["paths"][m.group(3)] = added + removed
    return commits


def user_emails(repo: Path, runner: Runner, extra: Iterable[str] = ()) -> set[str]:
    emails = {e.strip().lower() for e in extra if e.strip()}
    with contextlib.suppress(CommandError):  # Not set: only the extra addresses count.
        emails.add(runner(["git", "-C", str(repo), "config", "user.email"]).strip().lower())
    emails.discard("")
    return emails


def repo_activity(
    repo: Path, start: datetime, end: datetime, runner: Runner, extra_emails: Iterable[str] = ()
) -> dict[str, Any]:
    """The user's commits in one repository, and the branches whose tip they moved."""
    emails = user_emails(repo, runner, extra_emails)
    everyone = "*" in emails
    out: dict[str, Any] = {"name": repo.name, "path": home(repo), "commits": [], "branches": []}
    if not emails:
        out["warning"] = f"{repo.name}: no git user.email, so commits can't be matched to you."
        return out
    lo, hi = start.timestamp(), end.timestamp()
    git = ["git", "-C", str(repo)]
    # git filters on commit time, which a rebase or amend moves later; the author time decides
    # the day, so look a little past the end and filter on that.
    until = end + timedelta(days=REBASE_SLACK_DAYS)
    args = ["log", "--all", f"--since={start.isoformat()}", f"--until={until.isoformat()}"]
    args += ["--no-merges", f"--max-count={MAX_LOG}", LOG_FORMAT, "--numstat"]
    mine = [c for c in parse_git_log(runner([*git, *args])) if everyone or c["email"] in emails]
    out["commits"] = sorted((c for c in mine if lo <= c["ts"] < hi), key=lambda c: c["ts"])
    refs = runner([*git, "for-each-ref", "refs/heads", REF_FORMAT])
    for line in refs.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[1].isdigit() or not lo <= int(parts[1]) < hi:
            continue
        mails = {parts[2].strip("<>").lower(), parts[3].strip("<>").lower()}
        if everyone or mails & emails:
            out["branches"].append(parts[0])
    return out


def collect_git(
    folders: Iterable[str],
    start: datetime,
    end: datetime,
    *,
    runner: Runner | None = None,
    emails: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """Activity of every repository under ``folders`` with commits or branches in the range.
    A commit is counted once, so worktrees and second clones of a repository don't repeat it."""
    warnings: list[str] = []
    folders = list(folders)
    for raw in folders:
        if not Path(raw).expanduser().is_dir():
            warnings.append(f"Code folder {raw} does not exist.")
    if runner is None:
        if not shutil.which("git"):
            return [], [*warnings, "git is not installed, so commits are not included."]
        runner = run_command
    repos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for repo in find_repos(folders):
        try:
            item = repo_activity(repo, start, end, runner, emails)
        except CommandError as exc:
            warnings.append(f"{repo.name}: {exc}")
            continue
        if item.get("warning"):
            warnings.append(item.pop("warning"))
        fresh = [c for c in item["commits"] if c["hash"] not in seen]
        if item["commits"] and not fresh:
            continue  # Another checkout of a repository already listed.
        seen.update(c["hash"] for c in fresh)
        item["commits"] = fresh
        if fresh or item["branches"]:
            repos.append(item)
    return repos, warnings


# Obsidian ---------------------------------------------------------------------------------------


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The YAML front matter of a note (the simple subset Obsidian writes) and the body."""
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    data: dict[str, Any] = {}
    key = ""
    for line in m.group(1).splitlines():
        if key and (item := re.match(r"\s*-\s+(.+)", line)):
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(_unquote(item.group(1)))
            continue
        if field := re.match(r"([A-Za-z_][\w -]*):\s*(.*)$", line):
            key = field.group(1).strip().lower()
            value = field.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                data[key] = [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
            else:
                data[key] = _unquote(value)
    return data, text[m.end() :]


def parse_when(value: Any) -> float | None:
    """A timestamp from a front matter date ("2026-10-06", "2026-10-06 14:30", ISO 8601)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone().timestamp()
    except ValueError:
        pass
    if m := re.match(r"(\d{4}-\d{2}-\d{2})", text):
        try:
            return _midnight(date.fromisoformat(m.group(1))).timestamp()
        except ValueError:
            return None
    return None


def note_tags(meta: dict[str, Any], body: str) -> list[str]:
    raw = meta.get("tags", meta.get("tag", []))
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    tags = [str(t).lstrip("#").strip() for t in raw if str(t).strip()]
    plain = re.sub(r"```.*?```|`[^`\n]*`", "", body, flags=re.S)
    tags += TAG.findall(plain)
    return list(dict.fromkeys(t for t in tags if t))


def _walk_notes(vault: Path) -> Iterable[Path]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))  # .obsidian, .trash
        for name in sorted(filenames):
            if name.endswith(".md") and not name.startswith("."):
                count += 1
                if count > MAX_VAULT_FILES:
                    return
                yield Path(dirpath) / name


def vault_activity(
    vault: Path, start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Notes created or modified in the range, and daily notes (``YYYY-MM-DD.md``) for it."""
    lo, hi = start.timestamp(), end.timestamp()
    notes: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    for path in _walk_notes(vault):
        try:
            st = path.stat()
        except OSError:
            continue
        day = None
        if m := DAILY_NOTE.fullmatch(path.stem):
            try:
                day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                day = None
        is_daily = day is not None and lo <= _midnight(day).timestamp() < hi
        # A note created in the range was last written after its start.
        if not is_daily and st.st_mtime < lo:
            continue
        try:
            with path.open("rb") as fh:
                text = fh.read(MAX_NOTE_BYTES).decode("utf-8", "replace")
        except OSError:
            continue
        meta, body = frontmatter(text)
        # Only front matter says when a note was created: sync and copies reset file times.
        created = parse_when(meta.get("created")) or parse_when(meta.get("date"))
        if created is not None and lo <= created < hi:
            status, when = "created", created
        elif lo <= st.st_mtime < hi:
            status, when = "modified", st.st_mtime
        elif is_daily and day is not None:
            status, when = "daily", _midnight(day).timestamp()
        else:
            continue
        rel = path.relative_to(vault).as_posix()
        links = list(dict.fromkeys(link.strip() for link in WIKILINK.findall(body)))
        notes.append(
            {
                "ts": when,
                "vault": vault.name,
                "path": rel,
                "title": path.stem,
                "status": status,
                "words": len(body.split()),
                "tags": note_tags(meta, body),
                "links": [link for link in links if link],
            }
        )
        if is_daily and day is not None:
            daily.append(
                {
                    "date": day.isoformat(),
                    "vault": vault.name,
                    "path": rel,
                    "text": " ".join(OPEN_TASK.sub("", body).split()),  # Tasks go below.
                    "open_tasks": [t.strip() for t in OPEN_TASK.findall(body)],
                }
            )
    return notes, daily


def collect_vaults(
    vaults: Iterable[str], start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    notes: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw in vaults:
        vault = Path(raw).expanduser()
        if not vault.is_dir():
            warnings.append(f"Vault {raw} does not exist.")
            continue
        found, days = vault_activity(vault, start, end)
        notes += found
        daily += days
    notes.sort(key=lambda n: n["ts"])
    daily.sort(key=lambda d: d["date"])
    return notes, daily, warnings


# Bagley's own records ---------------------------------------------------------------------------


def chat_activity(store: Store, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Conversations with messages in the range."""
    rows = store.query(
        "SELECT c.id, c.title, c.mode, count(m.id) AS messages, "
        "min(m.created_at) AS first, max(m.created_at) AS last "
        "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.deleted_at IS NULL AND m.role IN ('user', 'assistant') "
        "AND m.created_at >= ? AND m.created_at < ? "
        "GROUP BY c.id ORDER BY first",
        (start.timestamp(), end.timestamp()),
    )
    automated = {a["conversation_id"] for a in store.list_automations() if a["conversation_id"]}
    return [
        {
            "id": r["id"],
            "ts": r["first"],
            "last": r["last"],
            "title": r["title"],
            "mode": r["mode"],
            "messages": r["messages"],
            "automation": r["id"] in automated,
        }
        for r in rows
    ]


def automation_activity(store: Store, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Automations whose last run falls in the range (reminders that fired, tasks that ran)."""
    lo, hi = start.timestamp(), end.timestamp()
    return sorted(
        (
            {
                "id": a["id"],
                "ts": a["last_run"],
                "kind": a["kind"],
                "name": a["name"],
                "status": a["last_status"] or "",
                "result": a["last_result"] or "",
            }
            for a in store.list_automations()
            if a["last_run"] and lo <= a["last_run"] < hi
        ),
        key=lambda a: a["ts"],
    )


# Putting it together ----------------------------------------------------------------------------


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M")


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _cut(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def summarize(raw: dict[str, Any], limits: dict[str, int] | None = None) -> dict[str, Any]:
    """Shape collected activity for a model or the UI: totals, per-day buckets, and lists cut to
    ``limits`` with a count of what was left out."""
    lim = {**LIMITS, **(limits or {})}
    start, end = raw["start"], raw["end"]
    repos, notes, daily = raw["repos"], raw["notes"], raw["daily"]
    chats, autos = raw["chats"], raw["automations"]
    commits = [(r["name"], c) for r in repos for c in r["commits"]]

    days: dict[str, dict[str, Any]] = {}

    def bucket(ts: float) -> dict[str, Any]:
        key = _day_key(ts)
        if key not in days:
            weekday = datetime.fromtimestamp(ts).strftime("%a").lower()
            days[key] = {
                "date": key,
                "day": weekday,
                "commits": 0,
                "added": 0,
                "removed": 0,
                "notes": 0,
                "chats": 0,
                "automations": 0,
                "repos": [],
                "subjects": [],
            }
        return days[key]

    for name, c in sorted(commits, key=lambda rc: rc[1]["ts"]):
        b = bucket(c["ts"])
        b["commits"] += 1
        b["added"] += c["added"]
        b["removed"] += c["removed"]
        if name not in b["repos"]:
            b["repos"].append(name)
        if len(b["subjects"]) < lim["subjects"]:
            b["subjects"].append(_cut(c["subject"], 90))
    for n in notes:
        bucket(n["ts"])["notes"] += 1
    for c in chats:
        bucket(c["ts"])["chats"] += 1
    for a in autos:
        bucket(a["ts"])["automations"] += 1

    repo_out = []
    for r in sorted(repos, key=lambda r: len(r["commits"]), reverse=True)[: lim["repos"]]:
        paths: Counter[str] = Counter()
        for c in r["commits"]:
            paths.update(c["paths"])
        log = r["commits"][-lim["log"] :] if lim["log"] > 0 else []
        item: dict[str, Any] = {
            "name": r["name"],
            "path": r["path"],
            "commits": len(r["commits"]),
            "added": sum(c["added"] for c in r["commits"]),
            "removed": sum(c["removed"] for c in r["commits"]),
            "files": len(paths),
            "branches": r["branches"][:8],
        }
        if lim["paths"] > 0:
            item["top_paths"] = [p for p, _ in paths.most_common(lim["paths"])]
        item["log"] = [
            {
                "time": _clock(c["ts"]),
                "subject": _cut(c["subject"], 120),
                "files": c["files"],
                "added": c["added"],
                "removed": c["removed"],
            }
            for c in log
        ]
        item["more"] = len(r["commits"]) - len(log)
        repo_out.append(item)

    tags: Counter[str] = Counter(t for n in notes for t in n["tags"])
    note_out = []
    shown = sorted(notes, key=lambda n: n["ts"], reverse=True)[: lim["notes"]]
    for n in sorted(shown, key=lambda n: n["ts"]):
        entry = {
            "time": _clock(n["ts"]),
            "vault": n["vault"],
            "path": n["path"],
            "status": n["status"],
            "words": n["words"],
            "tags": n["tags"][: lim["note_tags"]],
        }
        if lim["links"] > 0 and n["links"]:
            entry["links"] = n["links"][: lim["links"]]
        note_out.append(entry)
    daily_out = [
        {
            "date": d["date"],
            "vault": d["vault"],
            "path": d["path"],
            "excerpt": _cut(d["text"], lim["excerpt"]),
            "open_tasks": [_cut(t, 100) for t in d["open_tasks"][: lim["tasks"]]],
        }
        for d in daily[-lim["daily"] :]
        if lim["daily"] > 0
    ]
    chat_out = [
        {
            "id": c["id"],
            "time": _clock(c["ts"]),
            "title": _cut(c["title"], 80),
            "mode": c["mode"],
            "messages": c["messages"],
            **({"automation": True} if c["automation"] else {}),
        }
        for c in chats[-lim["chats"] :]
        if lim["chats"] > 0
    ]
    auto_out = [
        {
            "id": a["id"],
            "time": _clock(a["ts"]),
            "kind": a["kind"],
            "name": a["name"],
            "status": a["status"],
        }
        for a in autos[-lim["automations"] :]
        if lim["automations"] > 0
    ]
    return {
        "period": {
            "label": raw["label"],
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "days": max(1, round((end - start).total_seconds() / 86400)),
        },
        "totals": {
            "commits": len(commits),
            "repos": len(repos),
            "added": sum(c["added"] for _, c in commits),
            "removed": sum(c["removed"] for _, c in commits),
            "notes": len(notes),
            "daily_notes": len(daily),
            "chats": len(chats),
            "automations": len(autos),
        },
        "days": [days[k] for k in sorted(days)],
        "repos": repo_out,
        "repos_more": len(repos) - len(repo_out),
        "notes": note_out,
        "notes_more": len(notes) - len(note_out),
        "daily_notes": daily_out,
        "tags": dict(tags.most_common(lim["tags"])) if lim["tags"] > 0 else {},
        "chats": chat_out,
        "chats_more": len(chats) - len(chat_out),
        "automations": auto_out,
        "sources": raw["sources"],
        "warnings": raw["warnings"],
    }


async def collect(
    rt: Runtime,
    start: datetime,
    end: datetime,
    *,
    label: str = "",
    runner: Runner | None = None,
    emails: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Everything the user did in [start, end), unshaped (see ``summarize``)."""
    prefs, _ = rt.preferences()
    if emails is None:
        emails = [e for e in (rt.env.get("BAGLEY_GIT_EMAILS") or "").split(",") if e.strip()]
    (repos, git_warnings), (notes, daily, vault_warnings) = await asyncio.gather(
        asyncio.to_thread(
            collect_git, prefs.code_folders, start, end, runner=runner, emails=list(emails)
        ),
        asyncio.to_thread(collect_vaults, prefs.vaults, start, end),
    )
    warnings = git_warnings + vault_warnings
    if not prefs.vaults:
        warnings.append("No Obsidian vaults are set up (preference 'vaults').")
    return {
        "start": start,
        "end": end,
        "label": label or f"{start:%Y-%m-%d} to {end - timedelta(seconds=1):%Y-%m-%d}",
        "repos": repos,
        "notes": notes,
        "daily": daily,
        "chats": chat_activity(rt.store, start, end),
        "automations": automation_activity(rt.store, start, end),
        "sources": {"code_folders": list(prefs.code_folders), "vaults": list(prefs.vaults)},
        "warnings": warnings,
    }


async def activity(
    rt: Runtime,
    start: datetime,
    end: datetime,
    *,
    label: str = "",
    budget: int | None = None,
    runner: Runner | None = None,
    emails: Iterable[str] | None = None,
) -> dict[str, Any]:
    """The user's activity in [start, end) with per-day buckets. With ``budget``, lists are
    trimmed further until the JSON fits in that many characters."""
    raw = await collect(rt, start, end, label=label, runner=runner, emails=emails)
    data = summarize(raw)
    for level in SHRINK:
        if budget is None or len(json.dumps(data, ensure_ascii=False)) <= budget:
            break
        data = summarize(raw, level)
    return data


def open_threads(data: dict[str, Any], limit: int = 8) -> list[str]:
    """Hints for unfinished work: WIP commits, open tasks in daily notes, side branches."""
    hints: list[str] = []
    for repo in data["repos"]:
        hints += [
            f"{repo['name']}: {c['subject']}" for c in repo["log"] if WIP.search(c["subject"])
        ]
        side = [b for b in repo["branches"] if b not in MAIN_BRANCHES]
        if side:
            hints.append(f"{repo['name']}: branches {', '.join(side)}")
    for day in data["daily_notes"]:
        hints += [f"{day['date']}: {task}" for task in day["open_tasks"]]
    return hints[:limit]


def render_markdown(data: dict[str, Any]) -> str:
    """A short Markdown readout of ``activity`` (the recap draft posted into its chat)."""
    t = data["totals"]
    period = data["period"]
    lines = [
        f"**ACTIVITY // {period['label'].upper()}** ({period['start'][:10]} to "
        f"{(datetime.fromisoformat(period['end']) - timedelta(seconds=1)):%Y-%m-%d})",
        "",
        f"- {t['commits']} commits in {t['repos']} repositories (+{t['added']} -{t['removed']})",
        f"- {t['notes']} notes, {t['daily_notes']} daily notes",
        f"- {t['chats']} chats, {t['automations']} automation runs",
    ]
    for repo in data["repos"]:
        lines += ["", f"**{repo['name']}**: {repo['commits']} commits"]
        lines += [f"- {c['time'][5:].replace('T', ' ')} {c['subject']}" for c in repo["log"]]
        if repo["more"]:
            lines.append(f"- and {repo['more']} more")
    if data["notes"]:
        lines += ["", "**Notes**"]
        lines += [f"- {n['path']} ({n['status']})" for n in data["notes"]]
    for warning in data["warnings"]:
        lines += ["", f"[WARN] {warning}"]
    return "\n".join(lines)


def count_notes(vault: Path) -> int:
    return sum(1 for _ in _walk_notes(vault))


def sources(code_folders: Iterable[str], vaults: Iterable[str]) -> dict[str, Any]:
    """The vaults and repositories recaps read from, for the settings screen."""
    vault_out = []
    for raw in vaults:
        path = Path(raw).expanduser()
        exists = path.is_dir()
        vault_out.append(
            {"path": raw, "exists": exists, "notes": count_notes(path) if exists else 0}
        )
    repos = [{"path": str(p), "name": p.name} for p in find_repos(code_folders)]
    return {"vaults": vault_out, "repos": repos}
