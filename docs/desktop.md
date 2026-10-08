# Bagley on the Hyprland desktop

Bagley can live on a Hyprland desktop running the ctOS Quickshell shell:

- **Overlay.** `SUPER+B` opens a centered panel styled like the ctOS rofi launcher. Type a question, press Enter, and the answer streams in under it with a status line (`» THINK // H4CH1 // qwen3:14b`). When a tool needs approval, `ALLOW` / `DENY` rows appear.
- **"See what you see".** `SUPER+SHIFT+B` sends a screenshot of the focused window, its title and app, the highlighted text and the clipboard to a vision model: "what's this error?" without copying anything.
- **Explain the selection.** `SUPER+ALT+B` explains the highlighted text (or the clipboard when nothing is highlighted).
- **Bar segment.** A small Bagley avatar in the ctOS bar with the state code (`IDLE`, `THINK`, `EXEC web_search`, `AWAIT`...) and the machine answering. Left click toggles the overlay, right click opens the web UI.
- **mako.** `bagley see` and `bagley explain` also answer as a notification, and approvals can be answered from one.

The files are in the repository under `desktop/`:

| Path | What |
| --- | --- |
| `desktop/quickshell/bagley/` | QML: `BagleyService.qml` (state and commands), `BagleyOverlay.qml`, `BagleySegment.qml`, `CornerFrame.qml`, and `shell.qml` (standalone overlay, plus the ctOS bar steps in its comments) |
| `desktop/hypr/bagley.lua` | Hyprland binds for the Lua config (`hyprland.lua`) |
| `desktop/hypr/bagley.conf` | The same binds for hyprlang (`hyprland.conf`) |
| `desktop/mako/bagley.ini` | mako section for Bagley's notifications in ctOS colours |

## Requirements

- Hyprland and [Quickshell](https://quickshell.org) 0.2 or newer (the ctOS bar already needs it).
- `grim` (screenshots), `wl-clipboard` (`wl-paste`), `libnotify` (`notify-send`) and `mako`.
- A vision model for `see`, for example `ollama pull qwen2.5vl:7b`. Bagley picks a model that can see images on its own; set one under Settings → Model to choose. Without one, `see` answers with the error and the hint to install one.
- The JetBrainsMono Nerd Font, which ctOS uses already.
- A running Bagley server (`bagley`, or as a user service). Without one, the commands run the turn in their own process, which is slower to start and can't ask for approvals (tools that need one are declined).

On Kali: `sudo apt install grim wl-clipboard libnotify-bin mako-notifier` and `ollama pull qwen2.5vl:7b`. Quickshell isn't packaged for Kali; build it from source as its site describes. The Nerd Font isn't packaged either: download JetBrainsMono from the Nerd Fonts releases into `~/.local/share/fonts` and run `fc-cache -f`.

Quickshell runs `bagley` from its `PATH`. When Hyprland starts Quickshell, `~/.local/bin` (pipx, `pip install --user`) may not be on it. Then set `BAGLEY_BIN` to the full path in the environment Quickshell starts with, for example `hl.env("BAGLEY_BIN", os.getenv("HOME") .. "/.local/bin/bagley")` in `hyprland.lua` (or `env = BAGLEY_BIN,/home/you/.local/bin/bagley` in hyprlang). `BAGLEY_URL` and `BAGLEY_TOKEN` work as for every other client, and right-clicking the segment opens `BAGLEY_URL` (default `http://127.0.0.1:8765`).

## Install

One command puts every desktop file in place: the QML next to your ctOS bar (or in
`~/.config/quickshell/bagley` without one), the Hyprland binds, the mako style, the zsh plugin and
the wake word listener's systemd unit. It prints the lines left to add to your own configs;
`--edit-configs` adds them for you, once, between `# >>> bagley` markers, after saving a
`.bagley-bak` copy.

```sh
bagley desktop install                  # everything; --only quickshell,hypr picks parts
bagley desktop install --edit-configs   # also edit ~/.zshrc, hyprland.lua/conf and mako's config
bagley desktop install --bar ~/ctOS/bar.qml --dry-run
bagley desktop path                     # where the files are, inside the installed package
```

The ctOS bar folder in `/opt` belongs to root, so there it prints the `sudo cp` to run instead.
The bar itself is yours to edit; the steps are below.

## Install the QML by hand

### Into the ctOS bar

The ctOS bar is `bar.qml` in `/opt/ctos` (or `~/ctOS` if you keep it in your home folder), started with `qs --path /opt/ctos/bar.qml`. Folders next to it are QML modules named `qs.<folder>`, so:

1. Copy the folder next to `bar.qml`:

   ```sh
   sudo cp -r desktop/quickshell/bagley /opt/ctos/
   ```

2. In `bar.qml`, add the import after the others:

   ```qml
   import qs.common
   import qs.common.components
   import qs.bar.components
   import qs.bagley
   ```

3. Inside the root `PanelWindow`, once, create the overlay:

   ```qml
   PanelWindow {
       id: root
       // ...
       BagleyOverlay {}
   ```

4. In the right-hand `Row` (`Layout.alignment: Qt.AlignRight`), put the segment first with a divider after it, so the right side reads `[BAGLEY][net][user][status]`:

   ```qml
   Row {
       Layout.fillHeight: true
       Layout.alignment: Qt.AlignRight

       BagleySegment {}
       Divider {}

       SimpleSegment {
           id: netSegment
           // ...
   ```

5. Restart the bar: `qs kill -p /opt/ctos/bar.qml`, then start it the way you usually do (`QT_SCALE_FACTOR_ROUNDING_POLICY=Round qs --path /opt/ctos/bar.qml`, or your own launcher script).

`CornerFrame.qml` in the Bagley folder is marked internal, so it never clashes with the bar's own `CornerFrame`.

### On its own

To try it first, or next to a bar you'd rather not edit, run the overlay as its own Quickshell instance:

```sh
mkdir -p ~/.config/quickshell
cp -r desktop/quickshell/bagley ~/.config/quickshell/bagley
qs -p ~/.config/quickshell/bagley/shell.qml
```

Start it with Hyprland: `hl.on("hyprland.start", function() hl.exec_cmd("qs -p ~/.config/quickshell/bagley/shell.qml") end)` in Lua, or `exec-once = qs -p ~/.config/quickshell/bagley/shell.qml` in hyprlang. The segment needs a bar to sit in; `shell.qml` loads only the overlay.

### IPC

The overlay registers the IPC target `bagley`. `qs ipc` has to name the instance the way it was started (`-p` with the same path):

```sh
qs ipc -p /opt/ctos/bar.qml call bagley toggle        # open or close
qs ipc -p /opt/ctos/bar.qml call bagley open
qs ipc -p /opt/ctos/bar.qml call bagley close
qs ipc -p /opt/ctos/bar.qml call bagley ask "how do I mount a remote folder?"
qs ipc -p /opt/ctos/bar.qml call bagley see           # screenshot + selection + clipboard
qs ipc -p /opt/ctos/bar.qml call bagley explain       # explain the highlighted text
qs ipc -p /opt/ctos/bar.qml call bagley cancel        # stop the answer
qs ipc -p /opt/ctos/bar.qml show                      # lists the target and its functions
```

## Hyprland binds

| Keys | Does |
| --- | --- |
| `SUPER+B` | Toggle the overlay |
| `SUPER+SHIFT+B` | See: ask about the focused window (answer in the overlay) |
| `SUPER+ALT+B` | Explain the highlighted text (answer in the overlay) |

Lua (`hyprland.lua`): copy `desktop/hypr/bagley.lua` to `~/.config/hypr/` and add `require("bagley")`. It contains:

```lua
local ipc = "qs ipc -p /opt/ctos/bar.qml call bagley "
hl.bind("SUPER + B", hl.dsp.exec_cmd(ipc .. "toggle"))
hl.bind("SUPER + SHIFT + B", hl.dsp.exec_cmd(ipc .. "see"))
hl.bind("SUPER + ALT + B", hl.dsp.exec_cmd(ipc .. "explain"))
```

hyprlang (`hyprland.conf`): copy `desktop/hypr/bagley.conf` to `~/.config/hypr/` and add `source = ~/.config/hypr/bagley.conf`:

```ini
$bagley = qs ipc -p /opt/ctos/bar.qml call bagley
bind = SUPER, B, exec, $bagley toggle
bind = SUPER SHIFT, B, exec, $bagley see
bind = SUPER ALT, B, exec, $bagley explain
```

Change the path if your bar is in `~/ctOS`, or point it at `~/.config/quickshell/bagley/shell.qml` for the standalone overlay. Without Quickshell at all, bind `bagley see` and `bagley explain` directly; they answer in mako. If `SUPER+B` is taken (a browser, say), pick other keys.

No window rule is needed: the overlay is a layer surface with the namespace `bagley`. Both files carry an optional, commented layer rule that blurs behind the panel like a translucent rofi.

## Using the overlay

| Key | Does |
| --- | --- |
| Enter | Ask. A follow-up continues the same conversation. |
| Ctrl+Enter | Ask with the screen attached (screenshot of the focused window, its title, selection, clipboard). With an empty input it asks "What's on my screen?". |
| Up | Recall the last question |
| Escape | Close. If nothing has arrived yet, the question is cancelled too; otherwise the answer keeps coming and is there when you reopen. |
| Ctrl+C | Stop the answer (when no text is selected in the input) |
| Ctrl+N | New conversation |
| Ctrl+Shift+C | Copy the reply, or the part selected with the mouse |
| PageUp / PageDown | Scroll the reply |
| Up / Down / Tab, Enter | While an approval waits: pick `ALLOW` or `DENY`, then answer (with an empty input). `DENY` is preselected. |

Clicking outside the panel closes it. The panel fades in and out over 100ms; with `CTOS_BAR_ANIMATIONS=reduced` (as ctOS on b1t runs) it doesn't fade. Approvals can also be answered from the mako notification, the web UI, the phone or `bagley approve`; the first answer wins.

The status line shows this turn's code and the machine and model answering, or Bagley's overall state when the overlay hasn't asked anything yet. Codes: `IDLE`, `THINK`, `REASON`, `TX` (writing), `EXEC <tool>`, `AWAIT` (approval), `DONE`, `ERROR`, `ABORT` (stopped), `NO SIGNAL` (server not reachable).

## The bar segment

A 19px avatar (the ctOS diamond hub with two satellite nodes, as in the web UI's mini avatar), the state code and the machine name in gray. The hub is ctOS gray, turns green on `DONE`, red on `ERROR`, blinks white on `AWAIT` and dims on `NO SIGNAL`. It follows `bagley status --follow --json`, which keeps reconnecting when the server restarts.

## Commands

The overlay runs these; they work from a terminal too.

```sh
bagley overlay-ask "how do I mount a remote folder?"      # streams the answer
bagley overlay-ask --json -c <conversation> -- "and then?"  # one JSON event per line
bagley overlay-ask --see -- "why is this red?"            # with the screen attached
bagley see                                                # "What's on my screen? If there's an error, explain it and how to fix it."
bagley see what does this config line do
bagley see --no-image                                     # title, selection and clipboard only
bagley explain                                            # the highlighted text, else the clipboard
```

| Flag | Commands | Does |
| --- | --- | --- |
| `--json` | all three | One JSON object per line instead of text |
| `-c`, `--conversation ID` | `overlay-ask`, `see` | Continue a conversation |
| `--notify` | `overlay-ask` | Also show the reply as a notification |
| `--see` | `overlay-ask` | Attach the screen, as `bagley see` does |
| `--no-image` | `see` | Leave the screenshot out |
| `--no-notify` | `see`, `explain` | No notification (they notify by default) |

Without `--json` the reply goes to stdout as it streams and progress (`» EXEC web_search`, approvals, warnings) to stderr. The exit code is 1 when the turn failed or there was nothing to ask about.

With `--json` every line is one agent event as `POST /api/ask` streams it (`conversation`, `run.start`, `model` with `machine` and `model`, `status`, `text.delta`, `message`, `tool.start`, `approval.request`, `approval.result`, `tool.end`, `notice`, `error`, `run.end`), plus:

- `{"type": "context", "image": true, "image_bytes": 245312, "window_title": "...", "app": "kitty", "selection_chars": 20, "clipboard_chars": 0, "notes": ["CLIPBOARD NOT TEXT (image/png)"]}` first, for `see`, `explain` and `--see`: what was captured. The overlay hides while the screenshot is taken and comes back on this line.
- `approval.request` carries `summary`, the tool's one-line description with its arguments filled in (``Run `ls -la` ``).
- `{"type": "notice", "message": "NO SERVER // RUNNING HERE"}` when no server answered and the turn runs in this process.
- `{"type": "error", "message": "NOTHING SELECTED. Highlight some text first.", "notes": [...]}` when there was nothing to ask about.

Notifications are sent by the command itself (`notify-send --app-name Bagley`), so they appear on the screen that was captured even when the server is an always-on runner on another machine. Their title is `BAGLEY // <MACHINE>`; failures use `BAGLEY // ERROR` at critical urgency.

## What gets sent

Only on a keypress, and only:

- a screenshot of the focused window (grim). If that fails, the focused output, then every output; a note says so. Screenshots too big for the server are sent as JPEG.
- the window title and app class (`hyprctl -j activewindow`),
- the highlighted text (the primary selection) and the clipboard, text only, up to 8000 characters each. Images and other binary clipboard contents are skipped, and so is anything a password manager marks as a password (`x-kde-passwordManagerHint`) or that looks like a key, a token or a generated password.

They go to your Bagley server like any message and are stored with the conversation (the screenshot under the data folder's captures). Bagley's routing decides which model sees them, local unless you set up otherwise.

## mako

Append `desktop/mako/bagley.ini` to `~/.config/mako/config` and run `makoctl reload`:

```ini
[app-name=Bagley]
font=JetBrains Mono 11
background-color=#0E0E0Eee
text-color=#CACACA
border-color=#D9D9D9
border-size=1
border-radius=0
padding=12
progress-color=over #202020
markup=0
actions=1
on-button-left=invoke-default-action
on-button-middle=exec makoctl menu -n "$id" rofi -dmenu -p BAGLEY
on-button-right=dismiss

[app-name=Bagley urgency=low]
border-color=#202020

[app-name=Bagley urgency=critical]
border-color=#FC3E38
default-timeout=0
```

Approval requests arrive as critical notifications (red border, they stay) with Allow and Deny actions; middle-click to pick one in rofi. The server sends them when desktop notifications are turned on in Bagley's settings. `markup=0` shows model text as written instead of parsing it as Pango markup.

## Troubleshooting

- **The segment says `NO SIGNAL`.** The server isn't reachable. Run `bagley status` in a terminal: it prints the same thing the bar reads. Start the server, or set `BAGLEY_URL` (and `BAGLEY_TOKEN`) for Quickshell when it runs elsewhere. If `bagley status` works in a terminal but the bar stays dark, Quickshell can't find `bagley`: set `BAGLEY_BIN`.
- **`SUPER+B` does nothing.** Run the bind's command in a terminal. `No running instances` means `-p` doesn't match how the shell was started (check `qs list --all`). `qs ipc -p ... show` should list `target bagley`; if not, `BagleyOverlay {}` isn't loaded (check `qs log -p ...` for QML errors).
- **Keys stop working.** The overlay holds the keyboard while it is open. Press Escape, click outside it, or run `qs ipc -p ... call bagley close`.
- **`HYPRLAND NOT RUNNING` / `NO WAYLAND SESSION`.** `see` and `explain` find Hyprland's socket and the Wayland display in `$XDG_RUNTIME_DIR` even when `HYPRLAND_INSTANCE_SIGNATURE` and `WAYLAND_DISPLAY` are missing (a systemd unit, SSH). These mean neither was found: run them in your Hyprland session, and check `hyprctl instances`.
- **`GRIM NOT INSTALLED` / `WL-PASTE NOT INSTALLED`.** Install `grim` and `wl-clipboard`. `see` still asks with whatever it could read.
- **The screenshot shows the whole screen.** grim couldn't capture the window's rectangle (the note says `CAPTURED THE FOCUSED OUTPUT`). This can happen with some fractional scales; the answer still works.
- **"... has no model that can see images".** Pull a vision model (`ollama pull qwen2.5vl:7b`) or pick one in Settings → Model. `bagley see --no-image` asks with the text context only.
- **No notification.** Check that mako is running and that `notify-send test` shows one. Bagley skips notifications when `notify-send` is missing or there is no session bus. `--no-notify` turns them off on purpose.
- **Approvals are declined at once.** No server was running, so the turn ran in the command's own process, which can't wait for an answer (the overlay shows `NO SERVER // RUNNING HERE`). Start the server.
- **Wrong font or glyphs.** The overlay and segment use the JetBrainsMono Nerd Font, as ctOS does. Install it from the Nerd Fonts releases into `~/.local/share/fonts`, then `fc-cache -f`.
