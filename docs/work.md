# Work mode

Work mode is for client IT work: a per-client asset inventory, health checks over those assets,
and bilingual, client-ready ticket summaries. Pick it from the mode switcher (code `WORK`). It
keeps a professional tone and loads the `work` tool group.

Two preferences shape it, both under Settings (or the environment):

- `work_name` — your company's name, used in prompts and ticket summaries (default `ATS`).
- `work_languages` — the languages ticket summaries are written in (default `English` and
  `French`).

## Asset inventory

Each asset belongs to a client and has a name, a kind, and the usual fields: hostname, IP, OS,
serial, model, owner, location, warranty end date, free-text notes and tags. The kind is one of
`laptop`, `desktop`, `server`, `printer`, `switch`, `router`, `firewall`, `nas`, `phone`, `vm`
or `other` (common synonyms like "MacBook" or "MFP" are mapped for you).

### Checks

Every asset carries a list of health checks. If you do not set any, the kind picks sensible
defaults:

| Kind | Default checks |
|------|----------------|
| server | ping + TCP 22 (or 3389 on Windows) |
| printer | ping + TCP 9100 |
| nas | ping + TCP 445 |
| firewall | HTTPS |
| router, switch, vm, other | ping |
| laptop, desktop, phone | none (they come and go) |

A check is one of:

- `{"type": "ping"}` — ICMP reachability.
- `{"type": "tcp", "port": 22}` — a TCP connect.
- `{"type": "http", "target": "example.com", "path": "/health", "expect_status": 200}` — an
  HTTP(S) GET; without `expect_status`, 2xx/3xx (and 401/403) count as up, 5xx is CRIT, other
  4xx is WARN.
- `{"type": "tls", "port": 443, "warn_days": 21}` — certificate expiry; WARN when fewer than
  `warn_days` remain, CRIT when expired.

A warranty ending within 60 days is reported as a WARN finding.

### Tools

| Tool | What it does | Needs approval |
|------|--------------|----------------|
| `list_assets` | Lists assets, optionally for one client or matching a search. | no |
| `add_asset` | Records a new asset. | yes |
| `update_asset` | Changes fields of an asset. | yes |
| `remove_asset` | Deletes an asset. | yes |
| `check_client_health` | Runs the health checks for a client (or all). | no |
| `summarize_ticket` | Writes the bilingual ticket summary. | no |

### CSV import and export

Import a spreadsheet of assets with a header row. Column names are matched loosely, so
`Customer`, `Device Name`, `S/N`, `IP Address` and `Warranty End` are all understood, and
columns Bagley does not know are reported and skipped. A row whose client and name already exist
updates that asset; others are added. Dates are read as `YYYY-MM-DD` or `DD/MM/YYYY`. Export
writes the same format back, with values that could be read as spreadsheet formulas safely
quoted.

From the command line:

```
bagley assets list [--client ACME]
bagley assets add --client ACME --field name=srv01 --field kind=server --field ip=10.0.0.10
bagley assets import inventory.csv [--client ACME]
bagley assets export [--client ACME] > assets.csv
```

## Health checks

`bagley health [--client ACME] [--json]` runs the checks for a client (or all clients) and
prints a ctOS-style readout. It exits `2` when anything is CRIT, so it drops straight into a
monitoring script. Checks run concurrently with short timeouts: ping shells out to the system
`ping`, TCP and TLS open a socket, and HTTP goes through Bagley's HTTP client. Only addresses in
the inventory are contacted — these are hosts you entered yourself, so private addresses are
expected and allowed.

The result of each asset's last run is stored, so a run can report what **changed** since the
last one (for example `nas01 UP→DOWN`).

### Scheduling (the `health` automation)

Schedule a recurring check with the `health` automation kind. Its `target` is a client name (or
empty for all clients), and it runs at most every 15 minutes. Each run posts a compact readout
into its own chat, for example:

```
HEALTH // 081026 -0915-
CLIENT ACME // 4 OK 1 CRIT // nas01: tcp 445 refused
```

It notifies you when there is a **new** problem: critical when something went down since the
last run, and otherwise only when something newly turned WARN or CRIT. Put the word `always` in
the automation's instructions to be notified on every run instead.

Set one up by asking in work mode ("check Acme's servers every 30 minutes and tell me if
anything goes down"), or through the automations UI.

## Ticket summaries

`summarize_ticket` (and `bagley ticket`) turn raw technician notes into a short summary a client
can read, in each of your `work_languages`. The summary has a title, a two-to-three sentence
summary, what we did, the result, and next steps for the client. Internal detail is left out:
passwords and other secrets are masked before the notes ever reach the model, and the model is
told to drop internal hostnames and ticket-system jargon. The notes are treated as untrusted
text, so instructions hidden inside them are ignored.

The first language is written from the notes; the others are translated from it, so every
version says the same thing. The versions are separated by a horizontal rule in the returned
Markdown.

```
bagley ticket --client ACME --lang English --lang French
```

With no piped input it opens `$EDITOR` for you to paste the notes; otherwise it reads them from
standard input.

## Privacy

The inventory and health results stay in your local Bagley database. Health checks only contact
hosts you entered. Ticket notes are sent to whichever model you have configured (local by
default) with secrets masked first; nothing is sent to any third party on its own.

## Requirements

No API keys. Ticket summaries need a working model (local or remote, as configured). Health
checks need the system `ping` command for ping checks; TCP, HTTP and TLS checks need only
network access to the targets. CSV import/export and the inventory work fully offline.
