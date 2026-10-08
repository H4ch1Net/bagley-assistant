from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from bagley import policy, toolroute
from bagley.tools import ToolError, build_registry
from bagley.tools import desktop_control as dc
from bagley.tools.desktop_control import Desktop, Result, find_hyprland, run_program

pytestmark = pytest.mark.anyio

CLIENTS = [
    {
        "address": "0x55aa01",
        "mapped": True,
        "class": "kitty",
        "title": "zsh",
        "workspace": {"id": 2, "name": "2"},
        "focusHistoryID": 1,
    },
    {
        "address": "0x55aa02",
        "mapped": True,
        "class": "firefox",
        "title": "Bagley docs — Mozilla Firefox",
        "workspace": {"id": 1, "name": "1"},
        "focusHistoryID": 0,
    },
    {
        "address": "0x55aa03",
        "mapped": True,
        "class": "kitty",
        "title": "lazygit",
        "workspace": {"id": 2, "name": "2"},
        "focusHistoryID": 2,
    },
    {"address": "0x55aa04", "mapped": False, "class": "ghost", "title": "", "focusHistoryID": 3},
]
INSTALLED = {"hyprctl", "playerctl", "wpctl", "nmcli", "powerprofilesctl", "kitty", "zed"}
INSTALLED |= {"lazygit", "firefox"}


class FakeRunner:
    """Answers commands from a table of argv prefixes (longest wins) and records every call."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, ...], Result] = {
            ("hyprctl", "-j", "clients"): Result(0, json.dumps(CLIENTS)),
            ("hyprctl", "dispatch"): Result(0, "ok"),
        }
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, argv: list[str], env: dict[str, str] | None, timeout: float) -> Result:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert 0 < timeout <= 60
        self.calls.append(argv)
        self.envs.append(env)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if tuple(argv[: len(prefix)]) == prefix:
                return self.answers[prefix]
        return Result(0, "")

    def dispatched(self) -> list[list[str]]:
        return [c[2:] for c in self.calls if c[:2] == ["hyprctl", "dispatch"]]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    runner = FakeRunner()
    installed = set(INSTALLED)
    env = {
        "HYPRLAND_INSTANCE_SIGNATURE": "abc123",
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "home-share"),
        "XDG_DATA_DIRS": str(tmp_path / "system-share"),
    }

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in installed else None

    monkeypatch.setattr(dc, "desktop", Desktop(runner=runner, which=which, env=env))
    runner.installed = installed
    runner.home = tmp_path
    return runner


async def call(tool, **args):
    return await tool.invoke(args, None)


def desktop_file(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


async def test_launch_app_only_opens_allowed_apps(fake):
    result = await call(dc.launch_app, command="lazygit", workspace=2)
    assert result == "Opened lazygit on workspace 2."
    assert fake.calls[-1] == [
        "hyprctl",
        "dispatch",
        "exec",
        "[workspace 2 silent] kitty --class ctos-term -e lazygit",
    ]
    assert fake.envs[-1]["HYPRLAND_INSTANCE_SIGNATURE"] == "abc123"

    await call(dc.launch_app, command="Zed")
    assert fake.dispatched()[-1] == ["exec", "zed"]

    for bad in ("rm -rf ~", "kitty -e rm -rf ~", "firefox; reboot", "$(reboot)", ""):
        with pytest.raises(ToolError):
            await call(dc.launch_app, command=bad)
    with pytest.raises(ToolError, match="obsidian is not installed"):
        await call(dc.launch_app, command="obsidian")
    with pytest.raises(ToolError, match="numbered 1 to 10"):
        await call(dc.launch_app, command="kitty", workspace=11)
    assert len(fake.dispatched()) == 2  # Nothing rejected reached Hyprland.

    fake.installed.discard("kitty")
    with pytest.raises(ToolError, match=r"kitty is not installed \(sudo apt install kitty\)"):
        await call(dc.launch_app, command="lazygit")


async def test_launch_app_knows_installed_applications(fake):
    user = fake.home / "home-share" / "applications"
    system = fake.home / "system-share" / "applications"
    desktop_file(
        user,
        "org.example.Notes.desktop",
        "[Desktop Entry]\nType=Application\nName=Notes\nName[de]=Notizen\nExec=notes-app %U\n",
    )
    desktop_file(
        system,
        "mc.desktop",
        "[Desktop Entry]\nType=Application\nName=Midnight Commander\nExec=mc\nTerminal=true\n",
    )
    desktop_file(system, "hidden.desktop", "[Desktop Entry]\nName=Hidden\nExec=hidden-app\n")
    desktop_file(user, "hidden.desktop", "[Desktop Entry]\nName=Hidden\nHidden=true\n")
    desktop_file(
        system, "helper.desktop", "[Desktop Entry]\nName=Helper\nExec=helper\nNoDisplay=true\n"
    )
    desktop_file(
        system,
        "link.desktop",
        "[Desktop Entry]\nType=Link\nName=Link\nURL=https://example.com\n",
    )

    await call(dc.launch_app, command="notes", workspace=3)
    assert fake.dispatched()[-1] == ["exec", "[workspace 3 silent] notes-app"]
    await call(dc.launch_app, command="org.example.Notes")
    assert fake.dispatched()[-1] == ["exec", "notes-app"]
    await call(dc.launch_app, command="midnight commander")
    assert fake.dispatched()[-1] == ["exec", "kitty --class ctos-term -e mc"]
    desktop_file(system, "meter.desktop", "[Desktop Entry]\nName=Meter\nExec=meter -z 100%% %F\n")
    await call(dc.launch_app, command="meter")
    assert fake.dispatched()[-1] == ["exec", "meter -z 100%"]
    for name in ("hidden", "helper", "link", "notizen"):
        with pytest.raises(ToolError, match="not an app Bagley may open"):
            await call(dc.launch_app, command=name)


async def test_windows_are_matched_by_class_title_or_address(fake):
    windows = await dc.list_windows.run({}, None)
    listed = json.loads(windows[0])
    assert [w["address"] for w in listed] == ["0x55aa02", "0x55aa01", "0x55aa03"]
    assert listed[0] == {
        "address": "0x55aa02",
        "class": "firefox",
        "title": "Bagley docs — Mozilla Firefox",
        "workspace": 1,
        "focused": True,
    }

    await call(dc.focus_window, match="lazygit")
    assert fake.dispatched()[-1] == ["focuswindow", "address:0x55aa03"]
    await call(dc.focus_window, match="KITTY")  # Several match: the most recent one.
    assert fake.dispatched()[-1] == ["focuswindow", "address:0x55aa01"]
    await call(dc.move_window, match="mozilla", workspace=4)
    assert fake.dispatched()[-1] == ["movetoworkspacesilent", "4,address:0x55aa02"]
    with pytest.raises(ToolError, match="2 windows match 'kitty'"):
        await call(dc.close_window, match="kitty")
    await call(dc.close_window, match="0x55aa03")
    assert fake.dispatched()[-1] == ["closewindow", "address:0x55aa03"]
    with pytest.raises(ToolError, match="No open window matches 'slack'"):
        await call(dc.focus_window, match="slack")
    with pytest.raises(ToolError, match="No window has the address"):
        await call(dc.close_window, match="0xdead")
    with pytest.raises(ToolError, match="numbered 1 to 10"):
        await call(dc.move_window, match="firefox", workspace=0)

    await call(dc.switch_workspace, workspace="3")
    assert fake.dispatched()[-1] == ["workspace", "3"]
    fake.answers[("hyprctl", "dispatch")] = Result(0, "Invalid dispatcher")
    with pytest.raises(ToolError, match="Hyprland refused: Invalid dispatcher"):
        await call(dc.switch_workspace, workspace=2)


async def test_media_volume_wifi_and_power(fake):
    assert await call(dc.media, action="next") == "Media: next."
    assert fake.calls[-1] == ["playerctl", "next"]
    fake.answers[("playerctl",)] = Result(1, "", "No players found")
    with pytest.raises(ToolError, match="No media player is running"):
        await call(dc.media, action="play")
    with pytest.raises(ToolError, match="must be one of"):
        await call(dc.media, action="rewind")

    fake.answers[("wpctl", "get-volume")] = Result(0, "Volume: 1.50\n")
    assert await call(dc.volume, level=400) == "Volume 150%."
    assert ["wpctl", "set-volume", "-l", "1.5", "@DEFAULT_AUDIO_SINK@", "150%"] in fake.calls
    await call(dc.volume, change=-10)
    assert ["wpctl", "set-volume", "-l", "1.5", "@DEFAULT_AUDIO_SINK@", "10%-"] in fake.calls
    fake.answers[("wpctl", "get-volume")] = Result(0, "Volume: 0.35 [MUTED]\n")
    assert await call(dc.volume, mute="true") == "Volume 35% (muted)."
    assert fake.calls[-2] == ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"]
    with pytest.raises(ToolError, match="Give a level"):
        await call(dc.volume)
    with pytest.raises(ToolError, match="not both"):
        await call(dc.volume, level=10, change=5)

    fake.answers[("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")] = Result(
        0, "HomeNet:802-11-wireless\nWork\\:5G:802-11-wireless\nOffice VPN:vpn\n"
    )
    assert await call(dc.wifi, action="connect", ssid="homenet") == "Connected to HomeNet."
    assert fake.calls[-1] == ["nmcli", "connection", "up", "id", "HomeNet"]
    await call(dc.wifi, action="connect", ssid="Work:5G")
    assert fake.calls[-1] == ["nmcli", "connection", "up", "id", "Work:5G"]
    for ssid in ("Cafe Free WiFi", "Office VPN", ""):
        with pytest.raises(ToolError):
            await call(dc.wifi, action="connect", ssid=ssid)
    assert not any("Cafe" in " ".join(c) for c in fake.calls)
    await call(dc.wifi, action="off")
    assert fake.calls[-1] == ["nmcli", "radio", "wifi", "off"]
    fake.answers[("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device")] = Result(
        0, "lo:loopback:connected (externally)\nwlan0:wifi:connected\n"
    )
    await call(dc.wifi, action="disconnect")
    assert fake.calls[-1] == ["nmcli", "device", "disconnect", "wlan0"]

    await call(dc.power_profile, profile="performance")
    assert fake.calls[-1] == ["powerprofilesctl", "set", "performance"]
    with pytest.raises(ToolError, match="must be one of"):
        await call(dc.power_profile, profile="turbo")


async def test_status_reports_each_part_and_missing_programs(fake):
    fake.answers.update(
        {
            ("hyprctl", "-j", "activeworkspace"): Result(0, '{"id": 2, "name": "2", "windows": 3}'),
            ("hyprctl", "-j", "activewindow"): Result(
                0, '{"address": "0x55aa01", "class": "kitty", "title": "zsh"}'
            ),
            ("wpctl", "get-volume"): Result(0, "Volume: 0.45\n"),
            ("nmcli",): Result(0, "no:Neighbours:40\nyes:Home\\:Net:72\n"),
            ("powerprofilesctl", "get"): Result(0, "balanced\n"),
        }
    )
    fake.installed.discard("playerctl")
    status = await dc.desktop_status.func()
    assert status["workspace"] == {"id": 2, "name": "2", "windows": 3}
    assert status["focused"] == {"class": "kitty", "title": "zsh", "address": "0x55aa01"}
    assert status["volume"] == {"percent": 45, "muted": False}
    assert status["wifi"] == {"connected": True, "ssid": "Home:Net", "signal": 72}
    assert status["power_profile"] == "balanced"
    assert status["media"] is None
    assert status["unavailable"] == {
        "media": "playerctl is not installed (sudo apt install playerctl)."
    }
    with pytest.raises(ToolError, match="playerctl is not installed"):
        await call(dc.media, action="pause")

    fake.installed.add("playerctl")
    fake.answers[("playerctl",)] = Result(0, "spotify\tPlaying\tDaft Punk\tVeridis Quo\n")
    status = await dc.desktop_status.func()
    assert status["media"] == [
        {"player": "spotify", "status": "Playing", "artist": "Daft Punk", "title": "Veridis Quo"}
    ]
    assert "unavailable" not in status


def test_hyprland_socket_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "LEGACY_SOCKETS", tmp_path / "legacy")
    run = tmp_path / "run"
    assert find_hyprland({"HYPRLAND_INSTANCE_SIGNATURE": "sig", "XDG_RUNTIME_DIR": str(run)}) == (
        "sig",
        None,
    )
    assert find_hyprland({"XDG_RUNTIME_DIR": str(run)}) is None
    for name, mtime in (("old", 1_000_000), ("new", 2_000_000)):
        (run / "hypr" / name).mkdir(parents=True)
        (run / "hypr" / name / ".socket.sock").touch()
        os.utime(run / "hypr" / name / ".socket.sock", (mtime, mtime))
    (run / "hypr" / "nosocket").mkdir()
    assert find_hyprland({"XDG_RUNTIME_DIR": str(run)}) == ("new", str(run))

    runner = FakeRunner()
    desktop = Desktop(runner=runner, which=lambda n: n, env={"XDG_RUNTIME_DIR": str(run)})
    desktop.dispatch("workspace", "1")
    assert runner.envs[-1]["HYPRLAND_INSTANCE_SIGNATURE"] == "new"
    desktop = Desktop(runner=runner, which=lambda n: n, env={"XDG_RUNTIME_DIR": str(tmp_path)})
    with pytest.raises(ToolError, match="Hyprland is not running"):
        desktop.dispatch("workspace", "1")


def test_default_runner_uses_argument_lists():
    result = run_program(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "a b; c"], None, 20
    )
    assert result == Result(0, "a b; c\n", "")
    with pytest.raises(ToolError, match="Could not run"):
        run_program(["bagley-no-such-program"], None, 5)
    with pytest.raises(ToolError, match="did not answer"):
        run_program([sys.executable, "-c", "import time; time.sleep(5)"], None, 0.2)


def test_registration(config):
    config.ensure_dirs()
    assert dc.available(config) == sys.platform.startswith("linux")
    registry = build_registry(config)
    registry.add_module(dc, "builtin")
    tools = {name: registry.get(name) for name in ("list_windows", "desktop_status", "launch_app")}
    assert tools["list_windows"].risk == tools["desktop_status"].risk == "safe"
    for name in ("switch_workspace", "focus_window", "move_window", "close_window", "launch_app"):
        assert registry.get(name).risk == "confirm" and registry.get(name).category == "desktop"
    for name in ("media", "volume", "wifi", "power_profile", "set_up_scene"):
        assert registry.get(name).risk == "confirm"
    assert {"list_windows", "desktop_status"} <= policy.READS_PRIVATE
    picked = toolroute.select_tools(
        list(registry.tools.values()), [{"role": "user", "content": "Set up coding mode"}]
    )
    assert {"set_up_scene", "launch_app", "run_routine"} <= {t.name for t in picked}
    picked = toolroute.select_tools(
        list(registry.tools.values()), [{"role": "user", "content": "What's 2+2?"}]
    )
    assert "launch_app" not in {t.name for t in picked}
