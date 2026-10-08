# Always-on runner

Automations only run while Bagley runs. A laptop that sleeps half the day misses them, so give
them a machine that stays on: an old laptop or a tablet like a Microsoft Surface running Linux.
The runner is a normal Bagley started by systemd. The laptop, the desktop and the phone reach it
over Tailscale.

A typical setup:

| Machine | Role |
| --- | --- |
| Surface (Linux) | Runner: Bagley as a service, automations, phone access |
| Laptop (b1t) | Its own Bagley for chat; its CLI and overlay can point at the runner |
| Desktop with GPU (H4CH1) | Model server the runner routes to (Settings > Machines) |
| Phone (S25 Ultra) | The installed web app and ntfy notifications, see [phone.md](phone.md) |

## Requirements

- Linux with systemd on the runner (for a Surface, the linux-surface kernel helps with power and
  the touch screen), Python 3.10 or newer and Bagley installed (`pip install -e .` in a checkout,
  or `pipx install`).
- Tailscale on every machine, signed in to the same tailnet.
- A model the runner can reach: a small local one, or the GPU desktop added under
  Settings > Machines. Unattended runs use whatever the runner's routing picks.
- macOS and Windows have no `bagley service`; `bagley service install` prints how to start
  `bagley serve --no-browser` at login instead.

## 1. Install the service

On the runner:

```sh
bagley service install --runner
systemctl --user daemon-reload
systemctl --user enable --now bagley
bagley service status
```

`install` only writes `~/.config/systemd/user/bagley.service`; it never runs `systemctl` or
`sudo` itself and prints every command instead. `--print` shows the unit without writing it,
`--host` and `--port` change where it listens (keep the default `127.0.0.1` and put Tailscale in
front, see below). The unit runs the Python you installed it with:

```ini
[Service]
Type=simple
WorkingDirectory=%h
EnvironmentFile=-%h/.config/bagley/env
ExecStart=/home/you/.venv/bin/python -m bagley serve --no-browser --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=3
```

If Bagley crashes, systemd starts it again after 3 seconds. Logs:
`journalctl --user -u bagley -f`. `bagley service uninstall` removes the unit and prints the
commands that stop it.

### Settings for the service

The service reads `~/.config/bagley/env` (mode 0600). `bagley` itself loads the same file, after
a `.env` in the current directory, which wins.

```sh
bagley env set BAGLEY_TAILSCALE_USERS=you@example.com
bagley env set BAGLEY_TOKEN=-        # "-" reads the value from stdin, so it stays out of history
bagley env show                      # tokens and keys are masked
bagley env unset BAGLEY_TOKEN
systemctl --user restart bagley      # after any change
```

## 2. Keep it running with nobody logged in

User services stop when you log out, and only start when you log in. Lingering starts them at
boot and keeps them running:

```sh
sudo loginctl enable-linger $USER
```

## 3. Keep it awake with the lid closed

`install --runner` leaves a logind drop-in in `~/.config/bagley/logind-runner.conf`:

```ini
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
IdleAction=ignore
```

Install it and reboot:

```sh
sudo install -Dm644 ~/.config/bagley/logind-runner.conf /etc/systemd/logind.conf.d/bagley-runner.conf
```

Desktop environments have their own power settings: turn off automatic suspend in GNOME or KDE
if the runner has a desktop session. For a runner that must never sleep, you can also mask the
sleep targets: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
A Surface that stays plugged in lasts longer with Battery Limit mode on (Surface UEFI).

Keep the runner's time zone the same as yours (`timedatectl set-timezone Europe/Lisbon`):
schedules like "daily at 08:00" use the runner's clock.

## 4. Reach it over Tailscale

```sh
bagley tailscale            # this machine's tailnet name and the steps
tailscale serve --bg --https=443 http://127.0.0.1:8765
bagley tailscale --apply    # writes BAGLEY_ALLOWED_HOSTS and BAGLEY_PUBLIC_URL to the env file
bagley env set BAGLEY_TAILSCALE_USERS=you@example.com
systemctl --user restart bagley
```

Bagley stays on `127.0.0.1`; `tailscale serve` publishes it to your tailnet only, at
`https://surface.<tailnet>.ts.net`, with a certificate. [phone.md](phone.md) explains the
identity check and the token in detail. `bagley tailscale --peers` also looks for Bagley on the
other machines.

## 5. Move automations to the runner

Tell the laptop's Bagley where the runner is (or use the web UI's settings):

```sh
bagley runner set https://surface.tail1234.ts.net --token -   # token only if the runner has one
bagley runner status
```

```
RUNNER       https://surface.tail1234.ts.net
LINK         [OK]   ONLINE  38 MS
VERSION      0.1.0
AUTOMATIONS  2
```

Then move automations:

```sh
bagley runner push --all     # every enabled automation
bagley runner push 3 5       # by id
```

For each one, the laptop creates the same automation (kind, name, instructions, schedule,
target) on the runner. Only when the runner confirms does the laptop disable its copy and note
where it went (`state.moved_to`). If the runner is unreachable or refuses, nothing changes on the
laptop. Run history, chats and a watcher's last snapshot stay behind: the runner starts fresh.

`bagley runner pull` copies the runner's automations to the laptop, disabled, so you can see
what runs there. Memories and routines come along, merged.

To move everything else by hand, export and import JSON. Ids, chats, state and run history are
left out; importing again skips what is already there (automations by kind, name, schedule and
target, memories by text, routines by name).

```sh
bagley export > bagley.json                       # --parts automations,memories,routines
BAGLEY_URL=https://surface.tail1234.ts.net bagley import bagley.json
```

The runner token is only ever sent to the runner URL, and redirects are not followed.

## Point the laptop's CLI and overlay at the runner

Every client command (`bagley ask`, `bagley status --follow` for the Quickshell bar, the overlay,
`bagley approve`) talks to `BAGLEY_URL`. Set it in your shell profile on the laptop:

```sh
export BAGLEY_URL=https://surface.tail1234.ts.net
export BAGLEY_TOKEN=...      # only if the runner has a token
```

Prefer the shell profile over the laptop's `~/.config/bagley/env`: the laptop's own Bagley reads
that file too, and a `BAGLEY_TOKEN` there would lock its local web UI behind the runner's token.
`bagley runner`, `export`, `import` and `notify test` act on the Bagley at `BAGLEY_URL` as well;
with `BAGLEY_URL` unset they act on the laptop's own Bagley.

## When the runner is down

- Automations moved there don't run anywhere: they are disabled on the laptop. When the runner
  comes back, each one that came due runs once, then follows its schedule again.
- `bagley runner status` and the web UI show `NO SIGNAL` with the reason.
- The phone app shows a `NO SIGNAL` page and retries every 15 seconds.
- `bagley ask` on the laptop answers locally when the runner doesn't. `bagley status --follow`
  prints `NO SIGNAL` and keeps retrying.
- To bring an automation back, enable it again on the laptop and delete it on the runner.
