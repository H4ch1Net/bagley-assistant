from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from bagley.agent import Agent, RunRequest
from bagley.tools import ToolError
from bagley.tools import shell as shell_tools
from bagley.tools.shell import SANDBOX_ENV, bwrap_args
from tests.mock_llm import Reply

HOME = PurePosixPath("/home/user")
WORKSPACE = HOME / ".bagley" / "workspace"
REAL_WHICH = shutil.which


def pairs(args: list[str], flag: str) -> list[str]:
    """The values following each occurrence of ``flag``."""
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def test_bwrap_args_hide_home_and_keep_the_workspace_writable():
    args = bwrap_args(WORKSPACE, network=False, home=HOME, hide=[HOME / ".bagley"])
    assert args[:3] == ["--die-with-parent", "--new-session", "--unshare-all"]
    assert "--share-net" not in args
    assert args[args.index("--ro-bind") : args.index("--ro-bind") + 3] == ["--ro-bind", "/", "/"]
    assert pairs(args, "--dev") == ["/dev"] and pairs(args, "--proc") == ["/proc"]
    # /tmp and the home folder are emptied, then the workspace comes back on top.
    assert pairs(args, "--tmpfs") == ["/tmp", str(HOME)]
    bind = args.index("--bind")
    assert args[bind : bind + 3] == ["--bind", str(WORKSPACE), str(WORKSPACE)]
    assert args.index(str(HOME)) < bind
    assert pairs(args, "--chdir") == [str(WORKSPACE)]
    assert "--clearenv" in args and args.index("--clearenv") < args.index("--setenv")
    env = {args[i + 1]: args[i + 2] for i, a in enumerate(args) if a == "--setenv"}
    assert env == SANDBOX_ENV and env["HOME"] == "/tmp/home"
    assert set(env) == {
        "PATH",
        "LANG",
        "HOME",
        "TERM",
        "MPLBACKEND",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
    }
    assert pairs(args, "--dir") == ["/tmp/home"]
    assert args[-2:] == ["--cap-drop", "ALL"]


def test_bwrap_args_network_and_extra_paths():
    args = bwrap_args(
        "/srv/ws",
        network=True,
        home=HOME,
        hide=[
            "/var/lib/bagley",
            "/srv/ws/.bagley",
            "/srv/ws",
            HOME / ".bagley",
            "/run/user/1000",
            "/run",
        ],
        extra_ro=[HOME / ".venv"],
        extra_rw=["/tmp/bagley-run-x"],
    )
    assert args[3] == "--share-net"
    tmpfs = pairs(args, "--tmpfs")
    # Hidden folders outside the workspace go before it, the ones inside it after it; the
    # workspace itself and folders under the home folder or another hidden one need nothing.
    assert tmpfs == ["/tmp", str(HOME), "/var/lib/bagley", "/run", "/srv/ws/.bagley"]
    order = [i for i, a in enumerate(args) if a in ("/var/lib/bagley", "/srv/ws/.bagley")]
    bind = args.index("--bind")
    assert order[0] < bind < order[1]
    assert pairs(args, "--ro-bind") == ["/", str(HOME / ".venv")]
    assert pairs(args, "--bind") == ["/srv/ws", "/tmp/bagley-run-x"]
    assert args.index(str(HOME / ".venv")) > bind


def test_python_dirs_never_expose_the_home_folder(tmp_path):
    home = tmp_path / "home"
    venv = home / "dev" / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("")
    (home / "bin").mkdir()
    (home / "bin" / "python").write_text("")
    data = home / ".bagley"
    assert shell_tools._python_dirs(str(venv / "python"), home, [data]) == [home / "dev" / ".venv"]
    assert shell_tools._python_dirs(str(home / "bin" / "python"), home, [data]) == [home / "bin"]
    assert shell_tools._python_dirs("/usr/bin/python3", home, [data]) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_resolver_file_is_bound_back_when_it_lives_in_run(tmp_path):
    target = tmp_path / "run" / "systemd" / "resolve" / "stub-resolv.conf"
    target.parent.mkdir(parents=True)
    target.write_text("nameserver 127.0.0.53\n")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "resolv.conf").symlink_to(target)
    conf = tmp_path / "etc" / "resolv.conf"
    assert shell_tools._resolver([tmp_path / "run"], conf) == [target.resolve()]
    assert shell_tools._resolver([tmp_path / "data"], conf) == []


@pytest.fixture
def shell_rt(config, make_runtime):
    config.enable_shell = True
    return make_runtime(sandbox="bwrap")


@pytest.fixture
def fake_run(monkeypatch):
    """Records the command lines instead of running them."""
    calls: list[list[str]] = []
    reply = {"code": 0, "text": "hi\n"}

    async def run(args, cwd, timeout, env=None):
        calls.append(list(args))
        return reply["code"], reply["text"]

    monkeypatch.setattr(shell_tools, "_run", run)
    run.calls, run.reply = calls, reply
    return run


def with_bwrap(monkeypatch, path: str | None = "/usr/bin/bwrap") -> None:
    monkeypatch.setattr(
        shell_tools.shutil, "which", lambda name: path if name == "bwrap" else REAL_WHICH(name)
    )


@pytest.mark.anyio
async def test_sandbox_refuses_without_bwrap(shell_rt, fake_run, monkeypatch):
    with_bwrap(monkeypatch, None)
    for name, args in (("run_command", {"command": "ls"}), ("run_python", {"code": "print(1)"})):
        with pytest.raises(ToolError, match="bubblewrap"):
            await shell_rt.registry.get(name).run(args, shell_rt.tool_context())
    assert fake_run.calls == []  # Never falls back to running unsandboxed.


@pytest.mark.anyio
async def test_sandboxed_command_is_flagged(shell_rt, fake_run, monkeypatch):
    with_bwrap(monkeypatch)
    out, ui = await shell_rt.registry.get("run_command").run(
        {"command": "echo hi"}, shell_rt.tool_context()
    )
    assert ui == {"sandboxed": True}
    assert '"sandbox": "bwrap, no network"' in out and '"exit_code": 0' in out
    [args] = fake_run.calls
    workspace = str(Path(shell_rt.config.workspace).resolve())
    assert args[0] == "/usr/bin/bwrap" and args[-3:] == ["/bin/sh", "-c", "echo hi"]
    assert pairs(args, "--chdir") == [workspace] and "--share-net" not in args
    data = str(Path(shell_rt.config.data_dir).resolve())
    assert data in pairs(args, "--tmpfs") or Path(data).is_relative_to(Path.home().resolve())
    if Path(
        "/run"
    ).is_dir():  # Sockets there (D-Bus, Docker) are reachable through read-only mounts.
        assert "/run" in pairs(args, "--tmpfs")

    shell_rt.update_preferences({"sandbox_network": True})
    out, _ = await shell_rt.registry.get("run_command").run(
        {"command": "ls"}, shell_rt.tool_context()
    )
    assert '"sandbox": "bwrap"' in out and "--share-net" in fake_run.calls[-1]


@pytest.mark.anyio
async def test_sandboxed_python_binds_its_runner(shell_rt, fake_run, monkeypatch):
    with_bwrap(monkeypatch)
    out, ui = await shell_rt.registry.get("run_python").run(
        {"code": "print(1)"}, shell_rt.tool_context()
    )
    assert ui == {"sandboxed": True} and '"sandbox": "bwrap, no network"' in out
    [args] = fake_run.calls
    runner = Path(args[-3])
    assert args[-4] == sys.executable and runner.name == "runner.py"
    assert str(runner.parent) in pairs(args, "--bind")  # Writable, for the figures.


@pytest.mark.anyio
async def test_sandbox_setup_failure_is_reported(shell_rt, fake_run, monkeypatch):
    with_bwrap(monkeypatch)
    fake_run.reply.update(code=1, text="bwrap: setting up uid map: Permission denied\n")
    with pytest.raises(ToolError, match=r"could not start \(setting up uid map"):
        await shell_rt.registry.get("run_command").run({"command": "ls"}, shell_rt.tool_context())


@pytest.mark.anyio
async def test_sandbox_off_runs_directly(config, make_runtime, fake_run):
    config.enable_shell = True
    rt = make_runtime()
    out, ui = await rt.registry.get("run_command").run({"command": "ls"}, rt.tool_context())
    assert ui == {} and "sandbox" not in out and fake_run.calls[0][0] != "/usr/bin/bwrap"


@pytest.mark.anyio
async def test_audit_log_records_sandboxed_runs(shell_rt, fake_run, monkeypatch, mock, recorder):
    with_bwrap(monkeypatch)
    mock.script = [Reply(tool_calls=[("run_command", {"command": "ls -la"})]), Reply(text="Done.")]
    await Agent(shell_rt).run(RunRequest(text="list files"), recorder.emit, recorder.approve)
    [entry] = shell_rt.store.list_audit()
    assert entry["tool"] == "run_command" and entry["decision"] == "approved"
    assert entry["sandboxed"] is True and entry["ok"] is True


# Real bubblewrap, when this machine has it ------------------------------------------------------


def _bwrap_works(tmp: Path) -> bool:
    bwrap = REAL_WHICH("bwrap")
    if not bwrap or sys.platform != "linux":
        return False
    probe = [bwrap, *bwrap_args(tmp, network=False, home=tmp), "/bin/true"]
    try:
        return subprocess.run(probe, capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.anyio
async def test_real_sandbox_hides_home_and_allows_workspace(tmp_path, monkeypatch, make_runtime):
    if not _bwrap_works(tmp_path):
        pytest.skip("bubblewrap is not installed or can't create namespaces here")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("SECRET")
    monkeypatch.setenv("HOME", str(home))
    rt = make_runtime(sandbox="bwrap")
    rt.config.enable_shell = True
    from bagley.tools import build_registry

    run = build_registry(rt.config).get("run_command")
    secret = home / ".ssh" / "id_ed25519"
    out, ui = await run.run(
        {"command": f"cat {secret}; echo ok > made.txt; pwd"}, rt.tool_context()
    )
    assert ui == {"sandboxed": True}
    assert "SECRET" not in out and "No such file" in out
    assert (rt.config.workspace / "made.txt").read_text() == "ok\n"
    db = rt.config.db_path
    out, _ = await run.run({"command": f"ls {db} || echo hidden"}, rt.tool_context())
    assert "hidden" in out
