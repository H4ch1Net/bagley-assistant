# Routines

A routine is a named, ordered list of tool calls with fixed arguments. You approve it once, when
you save it, and run it again later with one confirmation: from the web UI, the terminal, a chat
("run my morning routine", "set up coding mode") or on a schedule.

Example, the built-in **coding** scene:

| # | Tool | Arguments |
|---|---|---|
| 1 | `launch_app` | `{"command": "kitty", "workspace": 2}` |
| 2 | `launch_app` | `{"command": "zed", "workspace": 2}` |
| 3 | `launch_app` | `{"command": "lazygit", "workspace": 2}` |
| 4 | `switch_workspace` | `{"workspace": 2}` |

## How approval works

Routines are approved per routine, not per step:

1. **Saving a routine approves its exact steps.** Whether you save it in the UI, record it, or
   let Bagley save it with the `save_routine` tool (which asks you first and shows the steps),
   what you saved is what will run. The arguments are fixed; no model fills them in later.
2. **Running a routine asks once for the whole routine.** The UI and `bagley routine run` show
   every step and ask once; the steps then run without asking again. In a chat, `run_routine`
   and `set_up_scene` ask once too.
3. **A scheduled routine runs unattended.** When you schedule a routine, its saved steps run at
   the scheduled time with nobody watching, including steps whose tools normally ask first
   (opening apps, writing files, running commands if the shell tool is on). Only schedule
   routines whose steps you are happy to run unattended, and remember that editing a routine
   changes what its schedule runs.

Some things still apply at run time:

- **Permission tiers.** A step whose tool is set to *Deny* under **Settings → Tools**, or whose
  tool no longer exists (a removed plugin, an MCP server that is offline), fails with the reason.
- **Stops at the first failure.** A run stops at the first step that fails and skips the rest,
  unless that step is marked *continue on error*.
- **Audit log.** Every step is written to the audit log with source `routine`, the tool's tier
  and the decision `routine` (or `blocked` for steps that could not run).
- **No routines inside routines.** `run_routine`, `save_routine` and `set_up_scene` can't be
  steps.

After each run from the UI, the terminal or a schedule, Bagley posts a summary into a chat called
**Routines**: `[OK]`, `[FAIL]` or `[SKIP]` for each step with its result. Runs from a chat put the
same summary in that chat instead.

## Making a routine

- **From a chat.** Ask Bagley to do the things, then make a routine from that chat in the web
  UI: it lists every tool call in the chat that succeeded, and you tick the ones to keep (the
  ones that changed something are pre-selected).
- **By recording.** Start recording in a chat, ask Bagley to do the things, then stop and name
  the routine. Every tool call that succeeded in between becomes a step. If the name is taken,
  the recording keeps going so you can pick another name.
- **By asking.** "Save what you just did as a routine called evening": Bagley calls
  `save_routine` with the steps, and you see them in the approval prompt.
- **By hand**, in the UI's editor or through the API below.

Limits: up to 20 steps, names up to 60 characters (not just digits), and every step's arguments
must be valid for its tool when you save.

## Scenes

Scenes ("set up coding mode") are routines. `set_up_scene("coding")` looks for a routine named
"coding", "coding mode" or "coding scene". See [Desktop control](desktop-control.md).

## Scheduling

Schedule a routine like any other automation, with the kind `routine` and the routine's number
as the target (**Settings → Automations**, or `POST /api/automations`):

```json
{"kind": "routine", "name": "Coding", "schedule": "weekdays at 08:55", "target": "1"}
```

Bagley checks that the routine exists when you save the schedule and again each time it runs.
Each run notifies you with the result, as an important notification when a step failed.
Deleting a routine pauses its schedules.

## Terminal

```
bagley routine list [--json]       # saved routines
bagley routine show NAME [--json]  # a routine's steps
bagley routine run NAME [--yes]    # lists the steps and asks y/N once; --yes skips the question
bagley routine delete NAME
```

`NAME` is a routine's name (any case) or number. The commands use the running Bagley server
(`BAGLEY_URL`, `BAGLEY_TOKEN`) and work in-process when none is running. `run` exits with 0 when
every step ran and 1 when it stopped.

## HTTP API

Routines are returned as:

```json
{
  "id": 1, "name": "coding", "description": "...", "builtin": true,
  "created_at": 1760000000.0, "updated_at": 1760000000.0,
  "last_run": null, "last_status": null, "running": false,
  "steps": [
    {"tool": "launch_app", "arguments": {"command": "kitty", "workspace": 2}, "note": "",
     "summary": "Open kitty", "risk": "confirm", "available": true}
  ]
}
```

`last_status` is `ok`, `failed` or null. A step may also carry `"continue_on_error": true`.
`available` is false when the tool is missing or set to *Deny*.

| Method and path | Body | Answer |
|---|---|---|
| `GET /api/routines` | | list of routines |
| `POST /api/routines` | `{name, description, steps: [{tool, arguments, note?, continue_on_error?}]}` | 201, the routine; 422 with `detail` when a step is invalid |
| `GET /api/routines/{id or name}` | | the routine, or 404 |
| `PATCH /api/routines/{id or name}` | any of `{name, description, steps}` | the routine |
| `DELETE /api/routines/{id or name}` | | 204 |
| `GET /api/routines/candidates?conversation_id=` | | successful tool calls in that chat: `[{message_id, result_id, call_id, tool, arguments, result, created_at, summary, risk, available, suggested}]` |
| `POST /api/routines/record` | `{conversation_id, action: "start" \| "stop" \| "cancel", name?, description?}` | recording state; `stop` needs `name` and adds `routine` |
| `GET /api/routines/record?conversation_id=` | | `{conversation_id, recording, after_id?, started_at?, steps: [candidates]}` |
| `POST /api/routines/{id or name}/run` | `{source?: "web" \| "cli" \| ...}` | NDJSON stream, see below; 409 if it is already running |

The run stream sends one JSON object per line:

```
{"type": "routine.start", "routine_id": 1, "name": "coding", "total": 4}
{"type": "routine.step", "routine_id": 1, "total": 4, "index": 0, "tool": "launch_app",
 "ok": true, "result": "Opened kitty on workspace 2.", "note": "", "summary": "Open kitty",
 "duration_ms": 41}
...
{"type": "routine.end", "routine_id": 1, "name": "coding", "ok": true, "steps": [...],
 "total": 4, "failed_at": null, "conversation_id": "...", "headline": "4/4 steps OK",
 "text": "...", "duration_ms": 180}
```

The run keeps going if the client disconnects. Open windows receive `routines.changed` over the
WebSocket whenever routines are saved, changed, deleted or run.
