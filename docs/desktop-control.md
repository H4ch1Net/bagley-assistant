# Desktop control

Bagley can control the Hyprland desktop it runs on: list and focus windows, move and close them,
switch workspaces, open apps, control media, change the volume, switch Wi-Fi and the power
profile, and set up whole scenes ("set up coding mode").

Reading the desktop is free. **Every change asks for your approval first**, unless you change
that tool's tier under **Settings → Tools** (for example, set `volume` and `media` to *Allow* if
you don't want to confirm "louder").

## Requirements

- Linux with [Hyprland](https://hyprland.org). On other systems the desktop tools are not
  loaded at all.
- The programs behind each tool. Bagley checks for each one and says clearly when it is missing;
  the rest keep working.

| Program | Used for | Arch package |
|---|---|---|
| `hyprctl` | windows, workspaces, opening apps | `hyprland` |
| `playerctl` | media (Spotify, browsers, mpv...) | `playerctl` |
| `wpctl` | volume | `wireplumber` |
| `nmcli` | Wi-Fi | `networkmanager` |
| `powerprofilesctl` | power profile | `power-profiles-daemon` |
| `kitty` | terminal apps (lazygit, btop, nvim...) | `kitty` |

### Running Bagley as a systemd user service

A user service does not inherit `HYPRLAND_INSTANCE_SIGNATURE`. Bagley finds the running Hyprland
instance by itself: it looks in `$XDG_RUNTIME_DIR/hypr/` (and `/tmp/hypr/` for Hyprland before
0.40) and uses the newest instance that has a `.socket.sock`.

The service also needs the session's `PATH` (to find the apps it may open) and the D-Bus session
address (for `playerctl`, `wpctl`, `nmcli` and `powerprofilesctl`). Most Hyprland setups export
them with this line in `hyprland.conf`:

```
exec-once = dbus-update-activation-environment --systemd --all
```

## What you can ask

| Ask | Tool | Approval |
|---|---|---|
| "What's open?", "Which windows are on workspace 2?" | `list_windows` | no |
| "What's playing?", "Am I on Wi-Fi?", "What's the volume?" | `desktop_status` | no |
| "Go to workspace 3" | `switch_workspace` | yes |
| "Focus Firefox", "Show me the lazygit window" | `focus_window` | yes |
| "Move Spotify to workspace 5" | `move_window` | yes |
| "Close the Discord window" | `close_window` | yes |
| "Open Obsidian", "Open lazygit on workspace 2" | `launch_app` | yes |
| "Pause the music", "Next track" | `media` | yes |
| "Volume to 40%", "Louder", "Mute" | `volume` | yes |
| "Turn Wi-Fi off", "Connect to HomeNet" | `wifi` | yes |
| "Battery saver", "Performance mode" | `power_profile` | yes |
| "Set up coding mode" | `set_up_scene` | yes, once for the whole scene |

The desktop tools load when a conversation mentions windows, workspaces, apps, music, volume,
Wi-Fi, power profiles or scenes, so small models are not handed them on every message.

## Windows

Windows are matched by their address (`0x...`, from `list_windows`), then by an exact app class
(`firefox`), then by part of the class, then by part of the title. When several windows match,
focusing picks the most recently used one. Moving and closing refuse to guess: Bagley lists the
matches and uses the exact address on the next try.

## Opening apps

Bagley only opens apps it knows, by name and without arguments:

- A built-in list: kitty, zed (runs `zeditor` on Arch), firefox, chromium, thunar, nautilus,
  dolphin, obsidian, spotify, code, discord, thunderbird, pavucontrol, and the terminal programs
  lazygit, btop, htop, nvim and yazi.
- The applications installed on this computer: every `.desktop` entry in your XDG data
  directories (`~/.local/share/applications`, `/usr/share/applications`, Flatpak exports), by
  its name ("Visual Studio Code") or desktop id (`org.gnome.Nautilus`). Hidden entries are
  skipped.

Anything else is refused, so the model can never run an arbitrary command this way. Apps open
through Hyprland (`hyprctl dispatch exec`), on a workspace if you name one
(`[workspace 2 silent]`, which does not switch you there). Terminal programs follow the ctOS
convention and open as `kitty --class ctos-term -e <app>`.

## Volume, Wi-Fi and power

- **Volume** applies to the default output (`@DEFAULT_AUDIO_SINK@`) and is kept between 0 and
  150%. Ask for a level, a change ("10 points louder") or mute/unmute.
- **Wi-Fi** can be switched on or off, disconnected, or connected to a network that is already
  saved on this computer (`nmcli connection up id <name>`). Bagley never sees or sends Wi-Fi
  passwords; join new networks in your system's network settings. If you reach Bagley from your
  phone over Wi-Fi, turning Wi-Fi off cuts that connection.
- **Power profile** is one of `power-saver`, `balanced` or `performance`.

## Scenes

A scene arranges the desktop for an activity. Scenes are [routines](routines.md), so you can
edit them or add your own under **Routines**.

The built-in **coding** scene opens kitty, Zed and lazygit (in kitty) on workspace 2, then
switches to workspace 2. Say "set up coding mode" (or "the coding scene") and approve once; the
four steps then run without asking again. It is added the first time routines are used on a
computer with the desktop tools; if you delete it, it stays deleted.

To make your own, open the apps you want with Bagley in a chat and save the steps as a routine,
or record them (see [Routines](routines.md)). A routine called "music" is then started with
"set up music mode".

## Automations

Scheduled tasks can't use these tools, because every change needs approval and nobody is there
to give it. To change the desktop on a schedule, save the steps as a routine and schedule the
routine: you approved those exact steps when you saved it.

## Troubleshooting

- **"Hyprland is not running"**: Bagley found no instance socket. Check that Hyprland is running
  for the same user as Bagley and that `XDG_RUNTIME_DIR` is set for the service.
- **"<program> is not installed"**: install the package from the table above, or make sure the
  service's `PATH` includes it.
- **An app does not open**: `launch_app` reports success when Hyprland accepted the command.
  Hyprland starts the app with its own environment; check `hyprctl` logs if it exits at once.
