<div align="center">

<img src="bagley/static/favicon.svg" width="88" alt="Bagley avatar">

# Bagley

A local-first AI assistant for your desktop, in the ctOS look: an animated avatar, tool use, a web UI,<br>
a Hyprland overlay and a terminal co-pilot. Runs on Ollama, any OpenAI-compatible server or Claude,<br>
routing each request to the best of your machines. Your conversations stay on your machine.

[![CI](https://github.com/H4ch1Net/bagley-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/H4ch1Net/bagley-assistant/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776ab)
![Runs locally](https://img.shields.io/badge/runs-locally-00fa9a)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

<img src="docs/screenshots/chat.png" alt="Bagley answering a weather question with a tool call, reasoning and a forecast table" width="900">

</div>

## Overview

Bagley is a personal assistant that runs against a model on your own computer. It is agentic: the model can call tools (search the web, read pages, check the weather, do exact maths, work with files, remember facts about you), look at the results and keep going until it has an answer. Actions that change things ask for your approval first.

Because it runs on your machine, it can do things a hosted chat app cannot. It keeps working when no window is open: reminders, scheduled tasks and page watchers run in the background and notify you. It searches your own documents by meaning, not just keywords. It edits files with a diff and a one-click undo, runs Python and shows the charts inline, and can check what is slowing your computer down.

It lives on the desktop, not only in a browser tab: `SUPER+B` opens a Quickshell overlay styled like the ctOS launcher, a bar segment shows its avatar and state, and replies arrive as mako notifications. It can see what you see (a screenshot of the focused window, the highlighted text and the clipboard go to a vision model), puts commands on your zsh prompt with `??`, explains the last failed command with `bagley why`, and briefs you every morning on failed services, disks, updates, new devices, open ports, failed SSH logins and vulnerable packages.

Heavy work goes to your GPU desktop over Tailscale, light work stays on the laptop, and a hosted API (Claude, OpenRouter, OpenAI and others, with your own key) takes over when neither answers. The readout always shows which machine and model is answering.

It is a single Python package with no build step. The server is FastAPI, the UI is plain JavaScript, and everything is stored in one SQLite file.

## Features

| | |
|---|---|
| **ctOS interface** | Ground, raised and chrome grays, one monospace face, square corners and white L-corner brackets; green and red only for state. Mode cells like workspaces, a boot log, mako-style toasts and rofi-style dialogs. A theme editor changes every colour token (accents, state colours, the avatar), with presets, contrast checks and a light theme derived from the same grays; the choice syncs to your other devices and the overlay. |
| **Animated avatar** | A small node graph seen through a tracking overlay: a diamond hub and a few satellite nodes inside a square viewport, joined by edges that carry signal packets, each node followed by a thin tracking box and ID. Traffic and readouts show what Bagley is doing: a slow trickle when idle, a dense mesh of packets while thinking, chained signals while reasoning, a scan line and the tool's name while a tool runs, a white blink while it waits for approval, a lock-on box when a reply lands, dashed, dimmed tracks when the model server is offline. |
| **Local models** | Native Ollama support (context size, reasoning, one-click model downloads) and any OpenAI-compatible server: LM Studio, llama.cpp, vLLM, LocalAI, Jan. |
| **Claude and API keys** | Claude through the official Anthropic SDK (Claude Opus 5.5 by default), with adaptive thinking, prompt caching and server-side refusal fallbacks. OpenAI, OpenRouter, Groq, Mistral, Gemini, DeepSeek and xAI are one click each: paste a key, test it, pick a model. Keys stay in the local database or come from the usual variables (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`...). |
| **Your machines** | Route each request to a GPU desktop over Tailscale, this laptop, a nearby machine or a cloud fallback, in that order, failing over when one doesn't answer. Titles and shell one-liners stay local. `bagley bench` tests every installed model on real tool-use tasks and picks the best one per machine. |
| **On the desktop** | A `SUPER+B` Quickshell overlay for quick questions, a ctOS bar segment with the avatar and state, replies and approvals as mako notifications. `bagley desktop install` puts it all in place. |
| **Sees your screen** | "What's this error?" grabs the focused window with grim and sends it to a vision model with the window title, the highlighted text and the clipboard. Another key explains the selection. |
| **Shell co-pilot** | `?? find files over 1GB` puts a command on your zsh prompt without running it, flagged when it is dangerous. `bagley why` explains the last failure from its output. |
| **Watchdog** | A morning briefing on failed systemd services, journal errors, disk and battery health and pending updates, plus new devices on your network, newly opened ports, failed SSH logins and known vulnerabilities in installed packages. To mako and your phone. |
| **Desktop control** | Hyprland windows and workspaces, media, volume, Wi-Fi and power profiles, each with approval. Scenes like "set up coding mode" (kitty, Zed and lazygit on workspace 2). |
| **Agent loop** | Multi-step tool calling with streaming, cancellation, step limits and approvals. Models without native function calling use a text-based tool protocol automatically. |
| **Built-in tools** | Web search, page reader, weather, calculator, time zones, workspace files, long-term memory, optional shell. |
| **Automations** | Reminders, scheduled tasks ("weekdays at 8:00, give me the weather and my calendar notes") and web page watchers ("tell me when this price drops"). They run in the background, post into their own chat and notify you, also as a desktop notification. Create them in chat or under **Settings → Automations**. |
| **Knowledge base** | Indexes your workspace and any folders you add (text, Markdown, code, HTML, PDF) into a local full-text index. With an embedding model installed (`ollama pull nomic-embed-text`) it also searches by meaning. Answers cite the files they came from. |
| **Undoable file changes** | Every write, edit, move and delete is journaled. Tool cards show a diff, and **Revert** restores the previous version. Deleted files are kept in the journal, not destroyed. |
| **Python and charts** | `run_python` runs a script in the workspace (after approval) and shows any chart or image it produces inline in the chat. Use it on your CSV and Excel files. |
| **Your computer** | Live CPU, memory, disk, battery and top processes, opening pages and files on your screen, and notifications. |
| **Small-model friendly** | Tools load on demand: core tools are always sent and the rest join when the conversation needs them, which roughly halves the prompt for small models. |
| **Model manager** | See which models are installed and which are in memory, and unload or delete them from **Settings → Model** (Ollama). |
| **Extensible** | Drop a Python file into the plugins folder, or connect any MCP (Model Context Protocol) server. |
| **Reasoning models** | Thinking from qwen3, deepseek-r1 or gpt-oss streams into a collapsible block, separate from the answer. |
| **Chat history** | Search, rename, delete with undo, Markdown export, edit and resend, regenerate, deep links. |
| **Attachments** | Drop text files on the composer. They are saved to the workspace where the file tools can read them. |
| **Voice** | A wake word, whisper.cpp dictation and replies in character with a British Piper voice or ElevenLabs. System voices in the browser too (the hub fires a signal with each word). |
| **Phone** | Over Tailscale, installed as an app on your phone, with ntfy pushes from automations that open the chat when tapped, and an Approvals shortcut on the app icon. |
| **Your life** | Your Obsidian vaults and the git repositories in `~/dev`: "what was I working on Tuesday?", and a weekly recap. |
| **Modes** | Study (flashcards with spaced repetition, quizzes from your notes), cybersec (CVE lookup, decoding, hash identification, port explanations, TLS and log analysis) and work. |
| **Work profile** | An asset inventory per client, remote health checks with alerts, and ticket notes turned into client-ready summaries in each of your working languages. |
| **Routines** | Record the actions you approve as a named routine, then replay it or put it on a schedule. |
| **Safer tools** | Permission tiers per tool (allow, ask, deny), a bubblewrap sandbox for commands and an audit log in ctOS terminal style. |
| **Always-on runner** | Hand automations to an always-on machine (a Surface, say) so they keep running while your laptop sleeps. |
| **Interface** | Keyboard shortcuts, responsive down to phone width, reduced-motion support, a tab-title marker when a reply finishes in the background. |
| **Terminal** | `bagley chat` for a terminal session, `bagley ask` for scripts and pipes, `bagley status` for bars, `bagley doctor` to check your setup. |

## Quick start

Bagley is built for Kali Linux (and works the same on Debian); the server and web UI run anywhere Python does.

**1. Run a model server.** The easiest is [Ollama](https://ollama.com/download), or skip this and use Claude or another hosted API with your key:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:4b
```

Any model works. Models with tool support give the best results; see [Choosing a model](#choosing-a-model).

**2. Install Bagley** (Python 3.10 or newer):

```bash
sudo apt install pipx && pipx ensurepath
pipx install git+https://github.com/H4ch1Net/bagley-assistant
# or: uv tool install git+https://github.com/H4ch1Net/bagley-assistant
# or, from a clone: pip install -e .
```

**3. Start it:**

```bash
bagley
```

This opens <http://127.0.0.1:8765>. If no model server is found, the start screen walks you through connecting one, and with Ollama you can download models from the UI.

**4. On the desktop** (optional): `bagley desktop install` sets up the Hyprland overlay, mako, the zsh co-pilot and the wake word listener, and `sudo apt install debsecan` gives the watchdog its vulnerability check. See the [guides](#guides).

## Screenshots

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/chart.png" alt="A chart made by run_python shown inside the chat"><br><sub>Python runs in the workspace and its charts appear in the chat.</sub></td>
    <td width="50%"><img src="docs/screenshots/diff.png" alt="A file edit with a diff and a Revert button"><br><sub>File changes show a diff and can be reverted.</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/automations.png" alt="Automations settings with a scheduled task, a reminder and a page watcher"><br><sub>Scheduled tasks, reminders and page watchers.</sub></td>
    <td><img src="docs/screenshots/knowledge.png" alt="Knowledge settings with indexed folders and search results"><br><sub>Knowledge base with keyword and semantic search.</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/screenshots/approval.png" alt="Approval prompt before writing a file"><br><sub>Actions that change things wait for approval.</sub></td>
    <td width="50%"><img src="docs/screenshots/empty.png" alt="Start screen with suggestions"><br><sub>Start screen.</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/settings.png" alt="Model settings"><br><sub>Model settings, installed models and what is loaded in memory.</sub></td>
    <td><img src="docs/screenshots/onboarding.png" alt="Setup card when no model server is running"><br><sub>First run without a model server. The tracker reads NO SIGNAL.</sub></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/light.png" alt="Light theme"><br><sub>Light theme.</sub></td>
    <td align="center"><img src="docs/screenshots/mobile.png" alt="Phone layout" width="220"><br><sub>Phone layout.</sub></td>
  </tr>
</table>

## Usage

### Web UI

Type a message and press <kbd>Enter</kbd>. Tool calls appear as cards you can expand to see arguments and results. The panel on the right shows the avatar, the current state, the model, generation speed, context usage, enabled tools and saved memories. Hide it with <kbd>Ctrl</kbd> <kbd>.</kbd> and the avatar moves to the top bar.

| Shortcut | Action |
|---|---|
| <kbd>Ctrl</kbd> <kbd>Shift</kbd> <kbd>O</kbd> | New chat |
| <kbd>Ctrl</kbd> <kbd>K</kbd> | Search chats |
| <kbd>/</kbd> | Focus the message box |
| <kbd>Esc</kbd> | Stop the current reply |
| <kbd>↑</kbd> | Edit your last message (in an empty message box) |
| <kbd>Ctrl</kbd> <kbd>Shift</kbd> <kbd>C</kbd> | Copy the last reply |
| <kbd>Ctrl</kbd> <kbd>.</kbd> | Toggle the Bagley panel |
| <kbd>Ctrl</kbd> <kbd>,</kbd> | Settings |
| <kbd>?</kbd> | Show all shortcuts |

On macOS use <kbd>⌘</kbd> instead of <kbd>Ctrl</kbd>.

### Command line

| Command | Description |
|---|---|
| `bagley` | Start the server and open the browser. Same as `bagley serve`. |
| `bagley serve --port 9000 --no-browser` | Start on another port without opening a tab. `--host` sets the bind address. |
| `bagley chat` | Chat in the terminal. `-c <id>` continues a conversation. Approvals are asked inline. |
| `bagley ask "question"` | Print one answer to stdout. Piped input is attached, so `git diff \| bagley ask "write a commit message"` works. Tools that need approval only run with `--yes`; `-q` hides tool activity. |
| `bagley doctor` | Check the model server, installed models, tool support, workspace, plugins and MCP servers. |
| `python -m bagley` | Same as `bagley`. |

### Tools

| Tool | What it does | Asks first |
|---|---|:---:|
| `web_search` | Search DuckDuckGo, or your SearXNG instance | |
| `fetch_webpage` | Download a page and extract readable text | |
| `get_weather` | Current weather and forecast from Open-Meteo | |
| `calculate` | Exact arithmetic and math functions, evaluated safely | |
| `get_current_time` | Date and time in any time zone | |
| `list_files` `read_file` `search_files` | Browse and read the workspace folder | |
| `write_file` `edit_file` `move_file` `delete_file` | Change files in the workspace. Journaled and revertible | ✓ |
| `make_directory` | Create a folder in the workspace | |
| `search_knowledge` `read_document` | Search and read your indexed documents | |
| `remember` `forget` | Long-term memory, injected into every conversation | |
| `set_reminder` `list_automations` `cancel_automation` | Reminders and managing automations | |
| `schedule_task` `watch_webpage` | Create a recurring task or a page watcher | ✓ |
| `system_status` | CPU, memory, disk, battery, uptime and the busiest processes | |
| `open_on_computer` | Open a web page or a workspace file on your screen | ✓ |
| `notify_user` | Show a notification | |
| `run_command` `run_python` | Run a shell command or a Python script in the workspace. Off unless `BAGLEY_ENABLE_SHELL=true` | ✓ |

Tools can be switched off individually in **Settings → Tools**. File tools cannot leave the workspace folder, and web tools refuse localhost and private network addresses unless you allow them.

### Automations

Ask in plain language ("remind me in 20 minutes to check the oven", "every weekday at 8:00 summarise the news on Rust", "watch this page and tell me when the price changes") or use **Settings → Automations**. Schedules accept forms like `in 45 minutes`, `at 18:30`, `tomorrow at 9am`, `every 2 hours`, `daily at 07:30`, `weekdays at 09:00` and `mondays, thursdays at 18:00`.

Automations run while Bagley is running. A run that was missed while it was off happens once at the next start. Unattended runs are restricted (see [Security](#security)); a refused tool call is recorded in the automation's chat.

### Knowledge base

The workspace is always indexed. Add more folders in **Settings → Knowledge**; the index refreshes every 15 minutes and on demand. Keyword search works out of the box. For search by meaning, install an embedding model (`ollama pull nomic-embed-text`); Bagley picks it up automatically. PDF support needs `pypdf` (`pipx inject bagley-assistant pypdf`, or `pip install -e ".[pdf]"` in a clone).

## Choosing a model

Bagley picks the first installed model that supports tools unless you choose one. Good starting points with Ollama:

| Model | Size | Notes |
|---|---|---|
| `qwen3:4b` | 2.5 GB | Fast, reliable tool use. Recommended default. |
| `llama3.2:3b` | 2.0 GB | Small and quick. |
| `qwen3:8b` | 5.2 GB | Better reasoning. |
| `gpt-oss:20b` | 14 GB | Best quality of these. Needs about 16 GB of memory. |

Models without native function calling (for example `gemma2`) still get tools through the text-based protocol. You can force a mode under **Settings → Model → Tool calling**.

### Other servers

Point Bagley at any OpenAI-compatible endpoint in **Settings → Model**, or with environment variables:

| Server | `BAGLEY_BASE_URL` |
|---|---|
| Ollama | `http://localhost:11434` |
| LM Studio | `http://localhost:1234/v1` |
| llama.cpp (`llama-server --jinja`) | `http://localhost:8080/v1` |
| vLLM | `http://localhost:8000/v1` |
| OpenRouter, OpenAI and other hosted APIs | the provider's base URL, plus `BAGLEY_API_KEY` |

With `BAGLEY_PROVIDER=auto` (the default) Bagley detects Ollama by its API, the Anthropic API by its address, and treats anything else as OpenAI-compatible.

### Claude and other hosted APIs

Under **Settings → Model & machines → Hosted APIs**, pick Claude (or OpenAI, OpenRouter, Groq, Mistral, Gemini, DeepSeek, xAI), paste your key and press **Test key**: it lists the models before anything is saved. The API becomes a cloud machine: by default the fallback when none of your machines answers, or the only route if you choose it under **Routing**. From the terminal:

```sh
export ANTHROPIC_API_KEY=sk-ant-...           # or save the key in Settings
bagley machines add Claude --preset claude     # Claude Opus 5.5; --model claude-sonnet-5-5 for another
bagley machines route                          # which machine and model answer now
```

To use Claude as this machine's model instead, set **Server type** to Anthropic (Claude), or `BAGLEY_PROVIDER=anthropic` and `BAGLEY_MODEL=claude-opus-5-5`. A key left empty is read from the provider's usual variable. Claude requests use prompt caching, send up to about 100,000 tokens of history, show the model's progress notes as reasoning, and on the Claude API retry a refused request on another Claude model server-side.

### Your machines

Add your other machines under **Settings → Model & machines** (or `bagley machines add H4CH1 http://h4ch1:11434`). With routing on **Automatic**, heavy work goes to GPU machines first, then this one, then nearby ones, then cloud APIs; a machine that doesn't answer is skipped for 45 seconds. Chat titles and shell help stay on this machine while **Keep light work on this machine** is on. The NODE readout and each reply show which machine answered. See [docs/bench.md](docs/bench.md) for picking the best model per machine.

## Configuration

Settings changed in the UI are stored in the database. Environment variables (or a `.env` file in the directory you start Bagley from) take precedence, and fields set that way are shown as locked in the UI. See [`.env.example`](.env.example) for every option.

<details>
<summary><b>Environment variables</b></summary>

| Variable | Default | Description |
|---|---|---|
| `BAGLEY_PROVIDER` | `auto` | `auto`, `ollama`, `openai` or `anthropic` |
| `BAGLEY_BASE_URL` | `http://localhost:11434` | Model server URL. `OLLAMA_HOST` is used if this is unset. |
| `BAGLEY_API_KEY` | | For hosted APIs. Empty: the provider's own variable below. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY` | | Keys for hosted APIs whose key isn't saved in Settings |
| `BAGLEY_MODEL` | first tool-capable model | Model name |
| `BAGLEY_TEMPERATURE` | `0.6` | 0 to 2 |
| `BAGLEY_CONTEXT_TOKENS` | `8192` | Context window. Sent to Ollama as `num_ctx`; history is trimmed to fit. |
| `BAGLEY_MAX_STEPS` | `8` | Tool rounds per reply |
| `BAGLEY_TOOL_MODE` | `auto` | `auto`, `native`, `prompt` (text-based) or `off` |
| `BAGLEY_THINK` | `false` | Let reasoning models think before answering |
| `BAGLEY_PERSONA` | `bagley` | `bagley`, `professional` or `concise` |
| `BAGLEY_HOST` | `127.0.0.1` | Bind address |
| `BAGLEY_PORT` | `8765` | Port |
| `BAGLEY_TOKEN` | | Access token. Generated automatically when listening beyond localhost. |
| `BAGLEY_ALLOWED_HOSTS` | | Extra Host header names, comma separated |
| `BAGLEY_DATA_DIR` | `~/.bagley` | Database, workspace, plugins |
| `BAGLEY_WORKSPACE` | `<data dir>/workspace` | Folder the file tools can use |
| `BAGLEY_PLUGINS_DIR` | `<data dir>/plugins` | Python files with extra tools |
| `BAGLEY_MCP_CONFIG` | `<data dir>/mcp.json` | MCP server list |
| `BAGLEY_ENABLE_SHELL` | `false` | Register the `run_command` and `run_python` tools |
| `BAGLEY_PYTHON` | Bagley's own | Python interpreter for `run_python`, e.g. a venv with pandas and matplotlib |
| `BAGLEY_ALLOW_PRIVATE_URLS` | `false` | Let web tools reach private addresses |
| `BAGLEY_SEARXNG_URL` | | Use SearXNG for web search |
| `BAGLEY_PUBLIC_URL` | | Address that notification clicks open, e.g. your Tailscale name for the phone |
| `BAGLEY_TAILSCALE_USERS` | | Tailscale logins allowed to use Bagley over the tailnet, comma separated |
| `BAGLEY_GIT_EMAILS` | from git config | Author emails counted in "Your life"; `*` counts everyone |
| `BAGLEY_WATCHDOG` | on Linux | `1` or `0` to force the watchdog tools on or off |
| `BAGLEY_URL`, `BAGLEY_TOKEN` | `http://127.0.0.1:8765` | For the clients (`bagley see`, the overlay, the zsh plugin, `bagley status`): the server to talk to |

</details>

Data lives in `~/.bagley`: `bagley.db` (chats, memories, settings, automations, file change journal), `knowledge.db` (search index), `journal/` (previous versions of changed files), `workspace/`, `plugins/` and `mcp.json`. Deleted chats can be restored with Undo; they are purged on the first start more than 24 hours after deletion.

## Guides

| | |
|---|---|
| [Desktop](docs/desktop.md) | The Quickshell overlay, bar segment, Hyprland binds and mako; `bagley see` and `bagley explain` |
| [Shell](docs/shell.md) | `??` in zsh and `bagley why` |
| [Watchdog](docs/watchdog.md) | The morning briefing and security checks |
| [Desktop control](docs/desktop-control.md) | Windows, workspaces, media, volume, Wi-Fi, power and scenes |
| [Voice](docs/voice.md) | Wake word, whisper.cpp, Piper and ElevenLabs |
| [Phone](docs/phone.md) | Tailscale, the installable app and ntfy |
| [Runner](docs/runner.md) | An always-on machine for automations |
| [Your life](docs/life.md) | Vaults, repositories and recaps |
| [Study](docs/study.md), [Cybersec](docs/cybersec.md), [Work](docs/work.md) | The modes |
| [Routines](docs/routines.md) | Recording and replaying approved actions |
| [Sandbox](docs/sandbox.md) | Permission tiers, bubblewrap and the audit log |
| [Bench](docs/bench.md) | Testing models on real tool-use tasks |

## Extending

### Plugins

Any Python file in `~/.bagley/plugins/` is loaded at startup. Decorate functions with `@tool`; type hints become the parameter schema and the docstring becomes the description the model sees.

```python
from typing import Annotated
from bagley.tools import ToolContext, tool


@tool(summary="Roll {count}d{sides}")
def roll_dice(sides: Annotated[int, "Faces per die"] = 6, count: int = 1) -> dict:
    """Roll dice and return each result and the total."""
    ...


@tool(risk="confirm")  # Ask the user before every call.
def send_report(ctx: ToolContext, text: str) -> str:
    """Write a report into the workspace."""
    (ctx.config.workspace / "report.md").write_text(text)
    return "Saved."
```

A complete example is in [`examples/plugins/example_tools.py`](examples/plugins/example_tools.py). Load errors are listed in **Settings → Tools** and in `bagley doctor`.

### MCP servers

Bagley is an MCP client for stdio servers. Create `~/.bagley/mcp.json` in the same format other MCP clients use (see [`examples/mcp.json`](examples/mcp.json)) and restart:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"]
    }
  }
}
```

Their tools appear as `<server>__<tool>`. Each call asks for approval unless the server has `"trust": true`.

## Security

Bagley can act on your behalf, so it is locked down by default:

- It listens on `127.0.0.1` only. When bound to another address it requires a token, which `bagley` prints in the URL.
- The server rejects foreign `Host` headers (DNS rebinding) and cross-origin requests and WebSocket connections, so other websites cannot drive it.
- File tools are confined to the workspace folder, symlinks included.
- Web tools refuse loopback and private network addresses, re-check every redirect, and connect to the exact address they checked, so DNS rebinding can't point them at your network. Downloads are capped in size.
- File changes, `run_command`, `run_python`, new scheduled tasks and watchers, and untrusted MCP tools wait for your approval. Shell and Python access are off unless you enable them, and stopping a command ends everything it started.
- Automations run without you watching. During those runs, tools that need approval are denied, and memories, automations and folders can't be changed. Once a run has read web content it can't read your files or notes, and it can only open pages from its search results. Watcher instructions run with no tools at all. Text on a web page therefore can't send your data anywhere.
- Every file change is journaled with the previous version, so a bad edit can be reverted.
- A saved API key is cleared when the server URL changes, and a key set through `BAGLEY_API_KEY` locks the server URL, so the key only goes where you configured it.
- The UI loads nothing from the internet and sends a strict Content-Security-Policy. Model output is sanitized: no images, frames or forms, so a prompt-injected page can't make the browser leak data.

## Architecture

```mermaid
flowchart LR
  UI["Web UI<br/>vanilla JS, canvas avatar"] <-- "WebSocket events" --> S["FastAPI server"]
  CLI["bagley chat / ask"] --> A
  S --> A["Agent loop"]
  SC["Scheduler<br/>tasks, reminders, watchers"] --> A
  S --> SC
  A <--> P{"Provider"}
  P --> O["Ollama /api/chat"]
  P --> OA["OpenAI-compatible /v1"]
  A --> T["Tool registry"]
  T --> B["Built-in tools"]
  T --> PL["Plugins"]
  T --> M["MCP servers"]
  B --> K[("Knowledge index<br/>FTS5 + embeddings")]
  A --> DB[("SQLite")]
```

Each user message starts an agent run. The agent builds the context (persona, date, memories, tool list, trimmed history), streams the model's reply, executes any tool calls (pausing for approval where needed) and loops until the model answers without tools or the step limit is reached. Every step is persisted, and events stream to the UI as they happen.

<details>
<summary><b>Project structure</b></summary>

```
bagley/
├── agent.py          Agent loop, context fitting, approvals, cancellation
├── automations.py    Schedule parser and background scheduler
├── cli.py            bagley serve | chat | ask | doctor
├── config.py         Server config and user preferences (env + database)
├── journal.py        File change journal, diffs and revert
├── knowledge.py      Document index: FTS5, embeddings, rank fusion
├── mcp.py            MCP stdio client
├── prompts.py        Personas, system prompt, text-based tool protocol
├── runtime.py        Shared state: store, tools, provider, scheduler, notifications
├── server.py         HTTP API, WebSocket sessions, security middleware
├── store.py          SQLite persistence
├── toolroute.py      On-demand tool loading
├── llm/              Ollama and OpenAI-compatible providers, stream parser
├── tools/            Registry, @tool decorator and built-in tools
└── static/           Web UI (HTML, CSS, JS modules, vendored marked and DOMPurify)
examples/             Plugin and MCP config examples
scripts/screenshots.py  Regenerates docs/screenshots against a mock model server
tests/                Unit, API, WebSocket and browser tests, mock model server
```

</details>

## Development

```bash
git clone https://github.com/H4ch1Net/bagley-assistant
cd bagley-assistant
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                 # unit, API and WebSocket tests
ruff check . && ruff format --check .
```

The tests run against `tests/mock_llm.py`, a scripted server that speaks both the Ollama and OpenAI wire formats, so no model or network access is needed.

Browser tests and screenshots use Playwright:

```bash
pip install -e ".[dev,screenshots]"
python -m playwright install chromium
pytest -m ui                          # drive the real UI in Chromium
python scripts/screenshots.py         # regenerate docs/screenshots
python scripts/screenshots.py --serve # demo at http://127.0.0.1:8790 with the mock model
```

The frontend has no build step. Edit the files in `bagley/static/` and reload the page.

Pull requests are welcome. Please run the tests and the linter, and keep new features covered by tests.

## Troubleshooting

<details>
<summary><b>"Can't reach the model server"</b></summary>

Start Ollama with `ollama serve` (the desktop app starts it automatically) and check the URL in **Settings → Model**. If Ollama runs on another machine or in Docker, set `BAGLEY_BASE_URL` to its address, and make sure Ollama listens beyond localhost (`OLLAMA_HOST=0.0.0.0`). `bagley doctor` shows exactly what fails.
</details>

<details>
<summary><b>The model never uses tools, or prints raw JSON</b></summary>

Use a model with tool support (see the table above). For other models set **Tool calling** to *Text-based*. Very small models (under 3B parameters) are often unreliable with tools.
</details>

<details>
<summary><b>Replies are slow, or the model forgets earlier messages</b></summary>

Lower the context window for speed or raise it for memory (both in **Settings → Model**). Turn off *Reasoning* for faster answers from thinking models. The Context row in the side panel shows how full the window was on the last reply.
</details>

<details>
<summary><b>Knowledge search only matches exact words</b></summary>

Semantic search needs an embedding model. Run `ollama pull nomic-embed-text` (or choose one under **Settings → Knowledge**) and press **Reindex**. With an OpenAI-compatible server, pick an embedding model your server provides.
</details>

<details>
<summary><b>run_python can't import pandas or matplotlib</b></summary>

It uses Bagley's own Python by default. Point `BAGLEY_PYTHON` at an interpreter that has your data libraries, for example a virtual environment's `bin/python`.
</details>

<details>
<summary><b>Web search returns nothing</b></summary>

DuckDuckGo occasionally rate limits automated searches. Run your own [SearXNG](https://docs.searxng.org/) with the JSON format enabled and set `BAGLEY_SEARXNG_URL`.
</details>

<details>
<summary><b>Using Bagley from a phone or another computer</b></summary>

Start it with `bagley --host 0.0.0.0` and open the printed URL, which includes an access token, from the other device. Set `BAGLEY_TOKEN` to keep the same token across restarts.
</details>

<details>
<summary><b>Port 8765 is already in use</b></summary>

Bagley is probably already running. Otherwise start it on another port with `bagley --port 8766`.
</details>

## License

[MIT](LICENSE). Bundles [marked](https://github.com/markedjs/marked) (MIT), [DOMPurify](https://github.com/cure53/DOMPurify) (Apache-2.0 or MPL-2.0) and icons from [Lucide](https://lucide.dev) (ISC).
