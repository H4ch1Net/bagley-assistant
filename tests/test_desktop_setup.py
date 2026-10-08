"""bagley desktop install: the desktop files land where each program looks."""

from __future__ import annotations

from pathlib import Path

from bagley import cli
from bagley.commands.desktop_setup import MARK_START, files_dir, install


def home_with(tmp_path: Path, *files: str) -> Path:
    home = tmp_path / "home"
    for name in files:
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# mine\n", encoding="utf-8")
    return home


def test_standalone_overlay_with_lua_hyprland_and_config_edits(tmp_path):
    home = home_with(tmp_path, ".zshrc", ".config/hypr/hyprland.lua", ".config/mako/config")
    report = install(home, edit=True, env={})

    shell = home / ".config/quickshell/bagley/shell.qml"
    assert shell.is_file() and (home / ".config/quickshell/bagley/BagleyOverlay.qml").is_file()
    assert (home / ".local/share/bagley/zsh/bagley.zsh").is_file()
    lua = (home / ".config/hypr/bagley.lua").read_text()
    assert f'local ipc = "qs ipc -p {shell} call bagley "' in lua
    assert (home / ".config/systemd/user/bagley-listen.service").is_file()

    zshrc = (home / ".zshrc").read_text()
    assert zshrc.startswith("# mine\n") and "source ~/.local/share/bagley/zsh/bagley.zsh" in zshrc
    assert 'require("bagley")' in (home / ".config/hypr/hyprland.lua").read_text()
    assert "[app-name=Bagley]" in (home / ".config/mako/config").read_text()
    assert (home / ".zshrc.bagley-bak").read_text() == "# mine\n"
    assert any("qs -p ~/.config/quickshell/bagley/shell.qml" in t for t in report.todo)

    again = install(home, edit=True, env={})  # Running it twice changes nothing.
    assert (home / ".zshrc").read_text().count(MARK_START) == 1
    assert "~/.zshrc already set up" in again.done


def test_into_the_ctos_bar_with_hyprlang_and_no_edits(tmp_path):
    home = home_with(tmp_path, "ctOS/bar.qml", ".config/hypr/hyprland.conf")
    report = install(home, only=["quickshell", "hypr"], env={})
    assert (home / "ctOS/bagley/BagleySegment.qml").is_file()
    conf = (home / ".config/hypr/bagley.conf").read_text()
    assert f"qs ipc -p {home / 'ctOS/bar.qml'} call bagley" in conf
    assert (home / ".config/hypr/hyprland.conf").read_text() == "# mine\n"
    assert "Add to ~/.config/hypr/hyprland.conf: source = ~/.config/hypr/bagley.conf" in report.todo
    assert any("import qs.bagley" in t for t in report.todo)
    assert not (home / ".local").exists()  # Only the parts asked for.


def test_dry_run_writes_nothing(tmp_path):
    home = home_with(tmp_path, ".zshrc")
    report = install(home, edit=True, dry_run=True, env={})
    assert report.done and (home / ".zshrc").read_text() == "# mine\n"
    assert not (home / ".config").exists()


def test_cli(tmp_path, monkeypatch, capsys):
    assert cli.main(["desktop", "path"]) == 0
    assert Path(capsys.readouterr().out.strip()) == files_dir()
    assert cli.main(["desktop", "install", "--only", "zsh,printer"]) == 2
    assert "Unknown part: printer" in capsys.readouterr().err
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert cli.main(["desktop", "install", "--only", "zsh"]) == 0
    out = capsys.readouterr().out
    assert "[OK]  zsh -> ~/.local/share/bagley/zsh" in out
    assert "[--]  Add to ~/.zshrc: source ~/.local/share/bagley/zsh/bagley.zsh" in out
