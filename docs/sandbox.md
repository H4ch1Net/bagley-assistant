# Sandbox, permissions and the audit log

Bagley can run shell commands (`run_command`) and Python scripts (`run_python`) when you start it
with `BAGLEY_ENABLE_SHELL=true`. Three layers keep that safe:

1. **Permission tiers** decide whether a tool runs on its own, asks you first, or is switched off.
2. **The sandbox** (bubblewrap) limits what an approved command can see and change.
3. **The audit log** records every tool call, who allowed it and how it went.

## The sandbox

With the sandbox on, both shell tools run inside [bubblewrap](https://github.com/containers/bubblewrap)
(`bwrap`), the same tool Flatpak uses. Nothing else changes: you still approve each run, and the
command still starts in the workspace folder.

| Inside the sandbox | |
|---|---|
| The system (`/usr`, `/etc`, `/opt`...) | Read-only |
| Your home folder | **Hidden** (an empty folder) |
| The workspace (`~/.bagley/workspace` or `BAGLEY_WORKSPACE`) | Read-write, even though it lives under the home folder |
| Bagley's data (`~/.bagley`: the database, captures, plugins, `mcp.json`) | **Hidden**, also when `BAGLEY_DATA_DIR` points outside the home folder |
| `/run` and `$XDG_RUNTIME_DIR` (D-Bus, Docker, Wayland and PipeWire sockets) | **Hidden** |
| `/tmp` | A private, empty folder; `HOME` is `/tmp/home` |
| `/dev`, `/proc` | Minimal; the command only sees its own processes |
| Network | **Off**, unless you allow it (see below) |
| Environment variables | Cleared: only `PATH`, `LANG`, `HOME`, `TERM`, `MPLBACKEND`, `PYTHONIOENCODING`, `PYTHONUTF8`. API keys and tokens in Bagley's environment never reach the command |
| Privileges | All capabilities dropped, a new session (no typing into your terminal), stopped when Bagley stops |

`run_python` also gets its interpreter: when it lives under your home folder (a virtualenv,
pyenv, conda), that folder is mounted read-only. Charts are still saved to `charts/` in the
workspace.

Why `/run` is hidden: a read-only mount still lets a program connect to the sockets in it. The
D-Bus session bus can ask your desktop to start programs outside the sandbox, and the Docker
socket is root access to the host.

The model sees `"sandbox": "bwrap, no network"` (or `"bwrap"`) in the result, so it knows why a
download failed. The tool card and the audit log carry a `BWRAP` marker.

### What it does not cover

- Other folders outside your home stay **readable** (for example `/mnt`, `/media`, `/srv`).
  Keep private data in your home folder, or leave the sandbox's network off.
- CPU and memory are not limited. Commands still stop after their timeout (120 s for
  `run_command`, 300 s for `run_python`), and output is capped.
- With the network on, a command can reach your local network and other machines on Tailscale.

### Requirements and setup

Linux with bubblewrap installed:

```sh
sudo pacman -S bubblewrap        # Arch
sudo apt install bubblewrap      # Debian, Ubuntu
sudo dnf install bubblewrap      # Fedora
```

Check that it works for your user:

```sh
bwrap --ro-bind / / --unshare-all /bin/true && echo OK
```

Ubuntu 24.04 and later restrict unprivileged user namespaces with AppArmor. If the check fails
with `setting up uid map: Permission denied`, the Ubuntu `bubblewrap` package's AppArmor
profile is missing or disabled; see Ubuntu's notes on
`kernel.apparmor_restrict_unprivileged_userns`.

Then turn it on in the preferences (`Settings`, or the API):

```sh
curl -X PUT http://127.0.0.1:8765/api/preferences \
  -H 'Content-Type: application/json' -d '{"sandbox": "bwrap", "sandbox_network": false}'
```

| Preference | Values | |
|---|---|---|
| `sandbox` | `"off"` (default), `"bwrap"` | Run the shell tools inside bubblewrap |
| `sandbox_network` | `false` (default), `true` | Let sandboxed commands use the network (`pip install`, `curl`, `git clone`) |

**If `bwrap` is missing while the sandbox is on, the shell tools refuse to run.** They never fall
back to running unsandboxed. The same goes when bubblewrap can't start (the error says why).
macOS and Windows have no bubblewrap: leave the sandbox off there.

## Permission tiers

Every tool has a tier:

| Tier | Meaning |
|---|---|
| `allow` | Runs without asking |
| `ask` | Asks you first, in every window, the overlay and the phone (`bagley approve` too) |
| `deny` | Never offered to the model |

The default comes from the tool's risk: tools that change your computer or saved data
(`run_command`, `run_python`, `write_file`, `open_on_computer`...) ask, the rest are allowed.
Override a tool with `tool_permissions` in the preferences, for example
`{"tool_permissions": {"run_python": "allow", "web_search": "deny"}}`.

Only set the shell tools to `allow` with the sandbox on. Unattended runs (automations) never use
a tool that asks by default, whatever its tier.

## The audit log

Every tool call is recorded: when, which conversation and client (`web`, `cli`, `phone`,
`automation`...), the arguments, the tier, the decision, whether it worked, how long it took and
whether it ran in the sandbox. Read it in the terminal:

```sh
bagley audit                      # the last 50 calls, oldest first
bagley audit -n 200 --tool run_command
bagley audit --conversation 3f2a9c1b04de
bagley audit --follow             # keep printing new calls (polls every 2 s)
bagley audit --json               # one JSON object per line
bagley audit -n 2000 --export audit.jsonl
```

```
071026-1643:07  EXEC  run_command   ASK>APPROVED  OK    412ms  BWRAP  {"command": "ls -la"}
071026-1643:09  EXEC  write_file    ASK>DENIED    --           phone  {"path": "notes/x.md"}
071026-1702:00  EXEC  web_search    AUTO          FAIL  1.2s          {"query": "lisbon weather"}
```

Columns: date and time (`ddMMyy-hhmm:ss`), the tool, the tier and decision (`ALLOW`/`ASK`/`DENY`
then `AUTO`/`APPROVED`/`DENIED`/`BLOCKED`; `AUTO` alone means it was allowed without asking),
the result (`OK`, `FAIL`, or `--` when it did not run), the duration, `BWRAP` when sandboxed, the
client when it was not the web UI, and the arguments cut to the terminal width. Colours follow
the ctOS palette and switch off with `NO_COLOR` or when the output is not a terminal.

`bagley audit` reads the running server (`GET /api/audit`, which takes `limit` up to 2000,
`after` (an entry id), `tool` and `conversation`); when no server runs it reads the database
directly.
