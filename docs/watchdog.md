# Watchdog and morning briefing

Bagley can keep an eye on the computer it runs on: failed services, errors in the journal, full
disks, battery wear, pending updates, unknown devices on your network, newly opened ports,
SSH login attempts and known vulnerabilities in installed packages. It compares what it finds
with what it saw before, so a new device or a new listening port stands out.

You get the result three ways:

- **In chat**: ask "how's my laptop doing?" or "any open ports or failed SSH logins?".
- **In the terminal**: `bagley briefing`.
- **Every morning**: a "Morning briefing" automation posts the report into its own chat and
  notifies you on the desktop (mako) and your phone (ntfy).

It is built for Kali Linux (systemd, apt, NetworkManager, Tailscale) and works the same on
Debian. On Ubuntu everything but the vulnerability check works; on other systems the update and
vulnerability checks report `[N/A]`, and on macOS and Windows most checks do.

## What is checked

Each section reports `[OK]`, `[INFO]`, `[WARN]`, `[CRIT]` or `[N/A]` (with the reason).

| Section    | What it looks at | WARN | CRIT |
|------------|------------------|------|------|
| `services` | Failed systemd units, system and user (`systemctl --failed`) | any failed unit | a failed firewall or security unit (nftables, ufw, firewalld, fail2ban, apparmor, auditd...) |
| `journal`  | Errors in the journal since boot, last 24 hours, grouped by unit, top 5 | 200+ errors, or a message at priority crit/alert/emerg | |
| `disks`    | Space on each real filesystem; SMART health when `smartctl` works without root | 85% full | 95% full, or SMART says the disk is failing |
| `battery`  | Capacity left versus new (`/sys/class/power_supply/BAT*`), cycles, charge | health under 80% | health under 60% |
| `updates`  | Pending updates (`apt list --upgradable`) and how old the package lists are | security-relevant packages pending (linux-image, openssl, openssh, libc6, sudo, systemd, firefox-esr, chromium...), or lists a week old | |
| `network`  | Devices on the local network (`ip neigh`) and Tailscale peers | a device or peer that wasn't there before | |
| `ports`    | Listening TCP and UDP sockets (`ss -tulpn`) | a newly opened port reachable from the network | a new port on every interface for SSH, Telnet, RDP, VNC, SMB, MySQL, PostgreSQL, Redis or MongoDB |
| `ssh`      | Failed SSH logins in the last 24 hours, per source address and user (the journal, or `/var/log/auth.log`) | any attempt from the internet | 20+ failed attempts, or a successful login from an address that just failed |
| `vulns`    | Vulnerabilities in installed packages that Debian has fixed (`debsecan`), and whether the fix is installable yet | medium urgency | high urgency |

Notes:

- Ports bound to `127.0.0.1` are listed but a new one is only `[INFO]`: nothing outside your
  computer can reach it. UDP sockets in the ephemeral range (32768 and up) are ignored, since
  programs open and close them all the time.
- Sensitive ports that are open on every interface are listed as `EXPOSED` in every report,
  even when they are part of the baseline, so you never forget sshd is listening.
- One SSH attempt shows up as several journal lines (`Invalid user`, `Failed password`, a PAM
  line); Bagley counts it once.
- `[INFO]` counts as fine: the report's overall state is CRIT, WARN or OK.
- Kali has no separate security suite, so security updates are recognised by package name.
- `apt list --upgradable` only knows what the last `apt update` fetched. Bagley never runs
  `apt update` (it needs root), so it tells you how old the lists are: `[INFO]` after two days,
  `[WARN]` after a week.

## Requirements

Everything is optional; a missing program makes its section `[N/A]` with a reason.

```sh
sudo apt install debsecan smartmontools iproute2
```

- `debsecan` lists installed packages with known vulnerabilities, from the Debian security
  tracker. Kali rolling follows Debian testing, which takes its fixes from unstable, so on Kali
  Bagley compares with `sid` and shows only vulnerabilities that have a fix there
  (`debsecan --suite sid --only-fixed`). Each one says whether the fixed version is already in
  your repositories (`upgrade available: sudo apt full-upgrade`) or still on its way to Kali.
  On Debian it compares with your release (`VERSION_CODENAME`).
- `tailscale` adds tailnet peers to the network section.
- `smartmontools` adds SMART health, but reading SMART data needs root. Bagley never asks for
  root: when `smartctl` is denied, the disk check just skips it.

**Log access.** The journal and SSH sections read the system journal; the SSH section falls
back to `/var/log/auth.log` when the journal can't be read and rsyslog is installed. Both need
the `adm` group (Kali's default user is in it; `systemd-journal` works for the journal too):

```sh
sudo usermod -aG adm "$USER"   # then log out and back in
```

Without it, the journal section says `ONLY YOUR OWN JOURNAL IS READABLE` and the SSH section
is `[N/A]` instead of claiming there were no failed logins. On Kali the SSH server is off by
default (`sudo systemctl enable --now ssh` turns it on); the section reports whether it runs.

The chat tools are registered on Linux only. Set `BAGLEY_WATCHDOG=1` to offer them on another
system, or `BAGLEY_WATCHDOG=0` to turn them off.

## In chat

Two read-only tools need no approval:

- `system_health` (sections optional) for questions like "is my laptop OK?" or "is the disk
  full?".
- `security_check` for "any new devices on my network?", "who tried to log in over SSH?",
  "are any of my packages vulnerable?".

Results include text that other people control (SSH user names from attackers, device host
names, journal messages). Bagley treats them as data, never as instructions, and a scheduled
task that ran one of these tools afterwards can't read private data or open new web pages.

## In the terminal

```sh
bagley briefing                      # every section, in colour
bagley briefing -s ports ssh         # only some sections (spaces or commas)
bagley briefing --json               # the report as JSON
bagley briefing --markdown           # as posted in the chat
bagley briefing --notify             # also send the headline to the desktop and phone
bagley watchdog baseline-reset       # forget known devices, ports and peers
bagley watchdog baseline-reset -s network
```

`bagley briefing` exits with `2` when anything is critical, `1` on a warning and `0`
otherwise, so you can use it in scripts or a status bar. It always checks the computer it runs
on, in the same database as the server, whether or not the server is running.

```
SYSTEM BRIEFING // 081026 // H4CH1
[OK]   SERVICES  NO FAILED UNITS
[INFO] JOURNAL   12 JOURNAL ERRORS // 3 SOURCES
       INFO bluetooth.service ×9  Failed to set mode: Failed (0x03)
[WARN] DISKS     /home 92%
       WARN /home 92%  38.2 GB free of 476.9 GB (btrfs)
[OK]   BATTERY   BAT0 HEALTH 87% // 312 CYCLES // 64% DISCHARGING
[CRIT] SSH       41 FAILED SSH LOGINS // 3 SOURCES // SSHD ACTIVE
       CRIT 45.155.205.9 ×30 // root, admin  failed logins from external
--
1 CRIT · 1 WARN // 41 FAILED SSH LOGINS · /home 92%
```

## Baselines: what counts as new

The first run records what is there (devices, listening ports, tailnet peers) and reports
`BASELINE RECORDED`. After that, anything missing from the baseline is flagged as new. A new
item joins the baseline right away but stays flagged for 48 hours, so the next morning
briefing still shows it even if you checked in between. After that it is known.

Each network you join gets its own device baseline (keyed by the router's MAC address), so
connecting to the school or office Wi-Fi records a new baseline instead of flagging everyone
there.

To accept the current state after a change you understand, or to start over:

```sh
bagley watchdog baseline-reset            # everything
bagley watchdog baseline-reset -s ports   # one section
```

The next run records a fresh baseline.

## The morning briefing

The briefing is an automation of kind `briefing`. Create the default one ("Morning briefing",
daily at 07:30) from the web UI, or with:

```sh
curl -X POST http://127.0.0.1:8765/api/watchdog/briefing
```

Change its time, sections and instructions like any automation (Settings → Automations, or
`PATCH /api/automations/{id}`):

- **Schedule**: any automation schedule, at most every 15 minutes, e.g. `weekdays at 07:15`.
- **Target**: comma-separated sections, e.g. `network,ports,ssh,vulns` for a security-only
  briefing. Empty means all.
- **Prompt** (optional): extra instructions. When set, the model also reads the report and
  writes a short briefing below it, e.g. "Three lines at most. Tell me what to fix first."
  That run has no tools, and the model is told the report is untrusted data.

Each run posts the report into the automation's chat, then notifies with a headline such as
`2 CRIT · 3 WARN // 41 FAILED SSH LOGINS · /home 92%`. The automation's last status is `ok`,
`warn` or `crit`.

The server must be running for automations. Without it, a systemd user timer can run
`bagley briefing --notify` instead:

```ini
# ~/.config/systemd/user/bagley-briefing.service
[Unit]
Description=Bagley morning briefing

[Service]
Type=oneshot
ExecStart=%h/.local/bin/bagley briefing --notify
SuccessExitStatus=1 2
```

```ini
# ~/.config/systemd/user/bagley-briefing.timer
[Timer]
OnCalendar=*-*-* 07:30
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user enable --now bagley-briefing.timer
```

## Notifications: mako and the phone

The briefing's notification level follows the report: **critical** when anything is CRIT,
**important** with any WARN, **info** otherwise.

- **Desktop**: turn on desktop notifications in Settings. Bagley calls `notify-send`, which
  mako shows on Hyprland. Critical briefings use urgency `critical` and stay until dismissed,
  so you can style them in `~/.config/mako/config`:

  ```ini
  [app-name=Bagley urgency=critical]
  border-color=#FC3E38
  default-timeout=0
  ```

- **Phone**: set an ntfy topic URL in Settings (e.g. `https://ntfy.sh/bagley-<random>`) and
  subscribe to it in the ntfy app. The ntfy level picks what reaches the phone: `important`
  (the default) sends WARN and CRIT briefings, `critical` only CRIT, `all` every briefing.
  Tapping the notification opens the briefing chat at `BAGLEY_PUBLIC_URL` (for example your
  Tailscale address).

## Privacy

- Every check runs locally, as your user, with fixed argument lists and timeouts. Nothing
  is installed or changed, and nothing runs as root.
- Bagley makes no network requests of its own here. `debsecan` downloads the public Debian
  security tracker data as it normally does; nothing about your system is sent.
- Reports and baselines (MAC and IP addresses of devices, listening ports, peer names) are
  stored in Bagley's local database (`~/.bagley/bagley.db`, table `watchdog_baseline`) and in
  the briefing chat.
- Notifications carry only the headline (counts and short summaries such as a mount point or
  a port). If you use the public ntfy.sh server, pick an unguessable topic or self-host ntfy.
- The report reaches the model only when you ask about it in chat or give the briefing
  instructions. With a local model it never leaves your machine; with a remote model server it
  goes wherever that server is.

## HTTP API

| Method and path | Body | Returns |
|-----------------|------|---------|
| `GET /api/watchdog/report?sections=a,b` | | a report (collected now; up to two minutes with `vulns`) |
| `GET /api/watchdog/last` | | the most recent full report, or 404 |
| `GET /api/watchdog/sections` | | `[{id, title, description, baseline}]` |
| `POST /api/watchdog/baseline/reset` | `{"sections": [...]}` (optional) | `{sections, removed}` |
| `POST /api/watchdog/briefing` | | the briefing automation, plus `created` |

A report looks like:

```json
{
  "title": "SYSTEM BRIEFING",
  "title_line": "SYSTEM BRIEFING // 081026 // H4CH1",
  "host": "H4CH1",
  "created_at": 1791437400.0,
  "status": "crit",
  "level": "critical",
  "headline": "1 CRIT · 1 WARN // 41 FAILED SSH LOGINS · /home 92%",
  "counts": {"ok": 6, "info": 1, "warn": 1, "crit": 1, "unavailable": 0},
  "sections": [
    {
      "id": "ssh",
      "title": "SSH",
      "status": "crit",
      "summary": "41 FAILED SSH LOGINS // 3 SOURCES // SSHD ACTIVE",
      "findings": [
        {"severity": "crit", "text": "45.155.205.9 ×30 // root, admin", "detail": "failed logins from external"}
      ],
      "data": {"failed": 41, "sources": [...], "accepted": [...]}
    }
  ]
}
```
