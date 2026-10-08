# Your own activity and weekly recaps

Bagley can answer "what was I working on Tuesday?" from your own data: the git commits in your code folders, the notes you wrote in your Obsidian vaults, your Bagley chats and the automations that ran. It can also write you a weekly recap every Friday.

Everything is read locally. Nothing is uploaded anywhere: the activity goes to the model you configured, and if that is a local model it never leaves your machine.

## Setup

Two preferences tell Bagley where to look:

| Preference | Default | What it is |
|---|---|---|
| `code_folders` | `["~/dev"]` | Folders that contain your git repositories. Bagley looks up to three levels down and skips `node_modules`, `.venv`, `target` and hidden folders. |
| `vaults` | `[]` | Paths to your Obsidian vaults (the folders that contain `.obsidian`). |

Set them in **Settings**, or through the API:

```sh
curl -X PUT http://127.0.0.1:8765/api/preferences \
  -H 'Content-Type: application/json' \
  -d '{"vaults": ["~/Notes"], "code_folders": ["~/dev", "~/work"]}'
```

`GET /api/life/sources` shows what Bagley found: each vault with its number of notes, and every repository under your code folders.

Vaults are added to the [knowledge base](../README.md#knowledge-base) as well, so `search_knowledge` and study mode can search your notes. The index picks up a new vault within 15 minutes, or straight away with **Reindex** in Settings → Knowledge.

### Which commits are yours

A commit counts when its author email matches `git config user.email` for that repository (which falls back to your global git config). If you commit under several addresses, list them in `BAGLEY_GIT_EMAILS`:

```sh
BAGLEY_GIT_EMAILS=me@example.com,me@work.example
```

`BAGLEY_GIT_EMAILS=*` counts every author, which is handy for repositories only you commit to. A repository with no email configured is skipped with a warning.

A commit is placed on the day you wrote it (its author date), so commits you rebased or amended later still show up on the right day. The same commit in several clones or worktrees is counted once.

## What it reads

| Source | What counts |
|---|---|
| Git | Commits by you in the period, on any branch (`git log --all --no-merges`), with files changed and lines added and removed; branches whose latest commit is yours and falls in the period. Requires `git` on your `PATH`. |
| Obsidian | Notes created in the period (going by `created:` or `date:` in the front matter, which Obsidian templates usually set) or modified in it (going by the file time). Daily notes named like `2026-10-06.md`, in any folder, count for their date and come with an excerpt and their open tasks (`- [ ] ...`). Tags (`#tag` and front matter `tags`) and `[[wikilinks]]` are listed. `.obsidian` and `.trash` are skipped. |
| Chats | Bagley conversations with messages in the period, with their title and mode. |
| Automations | Reminders that fired and tasks that ran in the period (their last run). |

## Asking about a day or a week

In any chat:

- "What was I working on Tuesday?"
- "What did I do yesterday?"
- "Summary of my last week"
- "Which commits did I make on 6 oct?"
- "Recap this week"

The `what_was_i_doing` tool accepts `today`, `yesterday`, weekday names, `last tuesday`, `this week`, `last week`, `past 7 days` (or any number of days or weeks), `3 days ago`, dates like `2026-10-06`, `6 oct` or `october 6`, month names, `this month` and `last month`. A weekday name means the most recent one, today included; `last tuesday` on a Tuesday means a week ago. `weekly_recap` takes a calendar week (`weeks_ago`: 0 for this week, 1 for last week) and adds a suggested structure: highlights, by project, notes written and open threads.

## The weekly recap

Turn it on from the web UI or with the API:

```sh
curl -X POST http://127.0.0.1:8765/api/life/recap-automation
```

This creates a "Weekly recap" automation (kind `recap`) that runs on Fridays at 17:00, unless one exists already. Change its schedule, pause it or run it now in **Settings → Automations** like any other automation. Each run collects the last 7 days, posts the raw activity as a draft in its own chat, and has the model write the recap from that data. The model gets no tools during the run, so text in your commit messages or notes can't make it do anything. You get a `WEEKLY RECAP` notification at the important level, which reaches your phone if you set up ntfy.

Extra instructions in the automation's prompt ("keep it under 200 words", "in French") are added to the request.

## Command line

```sh
bagley recap                    # today
bagley recap tuesday            # one day
bagley recap last week          # a range
bagley recap past 30 days --json
bagley recap last week --write  # a written recap from the model, on stdout
```

The readout uses the ctOS terminal style:

```
» ACTIVITY // tuesday 061026
  COMMITS 002  REPOS 001  NOTES 002  DAILY 001  CHATS 001  AUTO 000

  [GIT] bagley ~/dev/bagley  002 COMMITS  +412 -37
        -1015- Add flashcards table  02F +120 -2
        -1640- WIP: recap kind  03F +292 -35
        BRANCH feature/recap

  [NOTE] -0915- Brain/Projects/Lab.md  CREATED  #lab #cisco

  [DAILY] 061026 Brain/Daily/2026-10-06.md
        Subnetting lab, see [[VLSM]].
        [ ] finish lab 3

  [CHAT] -1100- Subnetting quiz  STDY  004 MSG
```

`bagley recap` asks the running server when there is one and reads the same sources itself otherwise. `--write` sends a compact version of the activity to the model (the server's, or the one in your settings) and prints only the recap on stdout, so `bagley recap last week --write > recap.md` works.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/life/activity?period=tuesday` | The activity for a period. `compact=1` trims the lists to what fits in a model prompt. A period Bagley can't read returns 422 with an explanation. |
| `GET /api/life/sources` | `{"vaults": [{"path", "exists", "notes"}], "repos": [{"path", "name"}]}` |
| `POST /api/life/recap-automation` | `{"created": bool, "automation": {...}}`. Creates the weekly recap unless one exists. |

The activity has `period`, `totals`, `days` (one entry per active day with counts and the first commit subjects), `repos` (with each repository's recent commits and a `more` count for the rest), `notes`, `daily_notes`, `tags`, `chats`, `automations`, `sources` and `warnings` (missing folders, git not installed, repositories without a git email). Times are local, as `YYYY-MM-DDTHH:MM`.

## Privacy

- Nothing is sent anywhere except to your model, and only when you ask a question or the recap runs.
- Unattended runs that have read web content can't call `what_was_i_doing` or `weekly_recap`, so instructions hidden in a web page can't pull your activity out.
- Bagley only runs `git` with fixed arguments (`log`, `config user.email`, `for-each-ref`) and never through a shell. It never writes to your repositories or vaults.
