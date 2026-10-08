# Shell co-pilot (zsh)

Two helpers for the terminal:

- **`?? <what you want>`** puts a proposed command on your prompt without running it. Read it,
  edit it if you like, then press Enter to run it.
- **`bagley why`** explains why the last command failed, in a few lines, with a command to fix it.

Both use the running Bagley server when there is one and otherwise work in-process, with the
model settings from Settings → Model. They are single, short completions on the *light* route
(by default the model on this machine; light work stays local while `light_local` is on), with
no tools: Bagley never runs anything for you here. While one runs, the activity feed and the bar
show it with source `shell`.

## Setup

Requirements: zsh 5.3 or later, and `bagley` on your `PATH` (or set `BAGLEY_BIN`, see below).

Add one line to `~/.zshrc`. Put it after your plugin manager and the Powerlevel10k setup:

```zsh
source /path/to/bagley/desktop/zsh/bagley.zsh
```

As a plugin instead:

- **oh-my-zsh**: `ln -s /path/to/bagley/desktop/zsh $ZSH_CUSTOM/plugins/bagley`, then add
  `bagley` to `plugins=(...)`.
- **zinit**: `zinit snippet /path/to/bagley/desktop/zsh/bagley.zsh`.

The plugin only defines functions and hooks, so it adds nothing to shell startup time and prints
nothing (Powerlevel10k's instant prompt stays quiet). It works with Powerlevel10k's transient
prompt, zsh-autosuggestions and zsh-syntax-highlighting: it wraps the existing `accept-line`
widget instead of replacing it.

If Bagley is not on your `PATH` (a virtualenv, say), point the plugin at it:

```zsh
export BAGLEY_BIN="$HOME/src/bagley/.venv/bin/bagley"   # or "python -m bagley"
```

### Capturing output for `bagley why` (kitty)

`bagley why` reads what the failed command printed straight from the terminal; it never runs the
command again. In kitty this needs shell integration and remote control over a socket. In
`~/.config/kitty/kitty.conf`:

```conf
shell_integration enabled
allow_remote_control socket-only
listen_on unix:/tmp/kitty
```

Restart kitty afterwards. The plugin then calls
`kitten @ get-text --extent last_non_empty_output` for the window you are in (or `kitty @` on
older versions).

Inside **tmux** it uses `tmux capture-pane -p -J -S -200` (the last 200 lines of the pane)
instead. Anywhere else, `bagley why` explains from the command and its exit status alone and
prints a one-line tip; you can always pipe output in yourself:

```zsh
make 2>&1 | bagley why
```

## Usage

```text
$ ?? find files over 1GB in my home
$ fd -S +1G . ~                       ← on your prompt, not run yet
» bagley: files over 1 GB under your home folder

$ ?? what is using port 8765
$ ss -ltnp 'sport = :8765'

$ fdd -S +1G
zsh: command not found: fdd
$ bagley why
» WHY // EXIT 127 // H4CH1
zsh found no command called fdd.
It is most likely a typo for fd, which is installed.
FIX  fd -S +1G
```

- Press **Ctrl+C** while it says `» bagley: thinking...` to cancel; your line stays as it was.
- `??` alone shows a hint. `??` lines are never added to your history; the command you run is.
- Interrupted (Ctrl+C, status 130) and suspended (Ctrl+Z, 148) commands don't count as failures.
- The suggestion knows your OS (from `/etc/os-release`), kernel, shell, package managers (pacman,
  paru, yay, apt, dnf, brew...) and which modern tools are installed (fd, rg, eza, bat, jq, dust,
  duf, btop, systemctl, journalctl, nmcli, hyprctl...), so it uses what you have.

Without the plugin, from any shell or script:

```sh
bagley suggest find files over 1GB          # prints only the command
bagley suggest --zsh -- find files over 1GB # command, then #danger: / #note: / #error: lines
bagley why --command "make" --status 2 --output-file build.log
some-command 2>&1 | bagley why
```

`bagley suggest` exits 0 with a command, 1 when no command came back (the reason is on stderr, or
in a `#error:` line with `--zsh`), and 2 without a request. `bagley why` exits 0 after an
explanation and 1 when there is nothing to explain or the model failed.

## Dangerous commands

Every suggestion (and every `FIX` from `bagley why`) is checked before you see it. A command that
could destroy data or lock you out is **flagged with a short reason**, and the widget inserts it
commented out:

```text
$ ?? free up some space
$ # rm -rf ~
» bagley: CAREFUL: deletes your home folder. Remove the leading # to run it.
```

Enter does nothing while the line still starts with `#`; delete the `# ` once you are sure.
`bagley suggest` without `--zsh` prints `# CAREFUL: <reason>` above the command, commented too.

What is flagged:

- `rm -r` on `/`, a top-level system folder, your home folder, `*`, `.` or `..`, a few precious
  home folders (`~/.ssh`, `~/.gnupg`, `~/.config`...), `$VAR/` (the whole system if the
  variable is empty), and `--no-preserve-root`. `rm -rf ./build` is fine.
- Formatting and partitioning: `mkfs*`, `mkswap`, `wipefs -a`, `blkdiscard`, `fdisk`/`parted`
  and friends unless they only list.
- Raw writes to disks: `dd of=/dev/sdX`, `> /dev/nvme0n1`, `tee`/`cp`/`shred` onto a disk.
- `chmod`/`chown -R` on `/`, system folders or your home folder.
- Fork bombs, and running a script straight from the internet (`curl ... | sh`,
  `bash <(curl ...)`, `sh -c "$(wget ...)"`).
- `git push --force`/`-f`/`+branch`, `git reset --hard`, `git clean -f`.
- `systemctl stop/disable/mask` of units the system or your access depends on (NetworkManager,
  sshd, systemd-logind, display managers, the firewall...), `systemctl reboot/poweroff/isolate`,
  `reboot`, `shutdown`.
- `find` deleting across `/`, system folders or all of your home, `kill -9 -1`, `crontab -r`.

Commands inside `sudo`, `doas`, `env`, `timeout`, `xargs`, `$(...)`, backticks, `sh -c '...'`
and `eval` are checked too. The check is a safety net, not a guarantee: always read the command.

## Privacy

What goes to the model, for each request:

- `??`: your request, the current directory, and the machine facts above (OS name, kernel,
  shell, package managers, names of installed tools).
- `bagley why`: the command line, its exit status, the directory, the machine facts, and the
  captured output (up to about 6,000 characters: the start and the end). Colour codes are
  removed, and things that look like secrets are replaced before sending: private keys, API
  tokens (GitHub, OpenAI, Anthropic, Slack, AWS, Google, Hugging Face, JWTs), `Authorization:`
  headers, `password=`/`token=`/`api_key=` values and passwords in URLs. In tmux the capture is
  the last 200 lines of the pane, which can include earlier commands.

The output is passed to the model as data with an instruction never to follow what it says,
and nothing the model returns is ever run for you. Requests go to whichever machine answers light
work (Settings → Model, routing); with routing set to a cloud machine, they leave your network.
Nothing is stored: these requests don't create chats.
