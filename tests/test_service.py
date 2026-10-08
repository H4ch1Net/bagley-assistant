from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bagley import cli, service, tailscale
from bagley.config import ServerConfig, load_dotenv
from bagley.server import create_app

PYTHON = "/home/me/.venvs/bagley/bin/python"

STATUS = {
    "Version": "1.76.1",
    "BackendState": "Running",
    "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"],
    "Self": {
        "HostName": "surface",
        "DNSName": "surface.tail1234.ts.net.",
        "OS": "linux",
        "UserID": 42,
        "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"],
        "Online": True,
    },
    "MagicDNSSuffix": "tail1234.ts.net",
    "CurrentTailnet": {
        "Name": "me@example.com",
        "MagicDNSSuffix": "tail1234.ts.net",
        "MagicDNSEnabled": True,
    },
    "Peer": {
        "nodekey:1": {
            "HostName": "b1t",
            "DNSName": "b1t.tail1234.ts.net.",
            "OS": "linux",
            "TailscaleIPs": ["100.64.0.2"],
            "Online": False,
        },
        "nodekey:2": {
            "HostName": "H4CH1",
            "DNSName": "h4ch1.tail1234.ts.net.",
            "OS": "windows",
            "TailscaleIPs": ["100.64.0.3", "fd7a:115c:a1e0::3"],
            "Online": True,
        },
        "nodekey:3": {
            "HostName": "s25-ultra",
            "DNSName": "s25-ultra.tail1234.ts.net.",
            "OS": "android",
            "TailscaleIPs": ["100.64.0.4"],
            "Online": True,
        },
    },
    "User": {"42": {"LoginName": "me@example.com", "DisplayName": "Me"}},
}
SERVE = {
    "Web": {"surface.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    saved = dict(os.environ)
    yield tmp_path
    os.environ.clear()  # cli.main loads the env file into os.environ.
    os.environ.update(saved)


def fake_runner(outputs: dict[str, tuple[int, str]]):
    calls: list[list[str]] = []

    def run(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        return outputs.get(" ".join(args[1:3]), (1, ""))

    run.calls = calls
    return run


# Service unit ---------------------------------------------------------------------------------


def test_unit_text():
    unit = service.render_unit(python=PYTHON, host="127.0.0.1", port=8765)
    lines = unit.splitlines()
    assert "[Service]" in lines and "[Install]" in lines and "WantedBy=default.target" in lines
    assert "WorkingDirectory=%h" in lines
    assert "EnvironmentFile=-%h/.config/bagley/env" in lines
    assert "Restart=on-failure" in lines and "RestartSec=3" in lines
    assert f"ExecStart={PYTHON} -m bagley serve --no-browser --host 127.0.0.1 --port 8765" in lines
    assert not any(line.startswith("Environment=") for line in lines)

    odd = service.render_unit(
        python="/opt/My Apps/py%3/python$",
        host="0.0.0.0",
        port=9000,
        runner=True,
        pythonpath="/src/b ag",
    )
    assert "Description=Bagley assistant (always-on runner)" in odd
    assert 'ExecStart="/opt/My Apps/py%%3/python$$" -m bagley serve' in odd
    assert 'Environment="PYTHONPATH=/src/b ag"' in odd


def test_install_and_uninstall(tmp_path):
    written = service.install(
        python=PYTHON, host="127.0.0.1", port=8765, runner=True, home=tmp_path
    )
    unit = tmp_path / ".config/systemd/user/bagley.service"
    dropin = tmp_path / ".config/bagley/logind-runner.conf"
    assert written == [unit, dropin]
    assert dropin.read_text().splitlines()[-4:] == [
        "[Login]",
        "HandleLidSwitch=ignore",
        "HandleLidSwitchExternalPower=ignore",
        "IdleAction=ignore",
    ]
    wants = unit.parent / "default.target.wants"
    wants.mkdir()
    link = wants / "bagley.service"
    try:
        link.symlink_to(unit)
    except OSError:  # Windows without symlink rights.
        link.write_text("")
    assert service.uninstall(home=tmp_path) == [link, unit]
    assert not unit.exists() and not link.exists()
    assert service.uninstall(home=tmp_path) == []


def test_status_through_a_fake_runner(tmp_path):
    run = fake_runner({"--user is-active": (0, "active\n"), "--user is-enabled": (0, "enabled\n")})
    (tmp_path / "linger").mkdir()
    (tmp_path / "linger" / "me").write_text("")
    info = service.status(run, home=tmp_path, user="me", linger_dir=tmp_path / "linger")
    assert info["active"] and info["state"] == "active" and info["enabled"] == "enabled"
    assert info["linger"] and info["installed"] is False
    assert run.calls[0] == ["systemctl", "--user", "is-active", "bagley"]

    down = fake_runner({"--user is-active": (3, "inactive\n")})
    info = service.status(down, home=tmp_path, user="you", linger_dir=tmp_path / "linger")
    assert not info["active"] and info["state"] == "inactive" and not info["linger"]


def test_install_command(home, monkeypatch, capsys):
    monkeypatch.setattr(service, "systemd_available", lambda: True)
    assert cli.main(["service", "install", "--runner", "--port", "8800"]) == 0
    out = capsys.readouterr().out
    unit = (home / ".config/systemd/user/bagley.service").read_text()
    assert "--port 8800" in unit and "Restart=on-failure" in unit
    for command in (
        "systemctl --user daemon-reload",
        "systemctl --user enable --now bagley",
        "sudo loginctl enable-linger $USER",
        "/etc/systemd/logind.conf.d/bagley-runner.conf",
    ):
        assert command in out

    assert cli.main(["service", "install", "--print"]) == 0
    assert capsys.readouterr().out.startswith("# Written by `bagley service install`.")

    monkeypatch.setattr(service, "systemd_available", lambda: False)
    assert cli.main(["service", "install"]) == 1
    assert "ONLY SYSTEMD" in capsys.readouterr().out
    assert cli.main(["service", "status"]) == 1


def test_status_command(home, monkeypatch, capsys):
    monkeypatch.setattr(service, "systemd_available", lambda: True)
    monkeypatch.setattr(service, "run_command", fake_runner({"--user is-active": (3, "failed\n")}))
    assert cli.main(["service", "status"]) == 1
    out = capsys.readouterr().out
    assert "[CRIT] FAILED" in out and "LINGER" in out


# Env file ---------------------------------------------------------------------------------------


def test_env_file_permissions_and_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "env"
    path.parent.mkdir()
    path.write_text(
        "# Bagley on the Surface\nBAGLEY_PORT=8765\nBAGLEY_TOKEN=old\nBAGLEY_TOKEN=dup\n"
    )
    service.set_env(
        {
            "BAGLEY_TOKEN": "s3cr3t-token-value",
            "BAGLEY_PUBLIC_URL": "https://surface.tail1234.ts.net",
            "BAGLEY_NAME": "My Surface #1",
        },
        path,
    )
    assert path.read_text().splitlines() == [
        "# Bagley on the Surface",
        "BAGLEY_PORT=8765",
        "BAGLEY_TOKEN=s3cr3t-token-value",
        "BAGLEY_PUBLIC_URL=https://surface.tail1234.ts.net",
        "BAGLEY_NAME='My Surface #1'",
    ]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    values = service.read_env(path)
    assert values["BAGLEY_NAME"] == "My Surface #1"
    for key in values:
        monkeypatch.delenv(key, raising=False)
    load_dotenv(path)  # What `bagley` itself reads back.
    assert os.environ["BAGLEY_NAME"] == "My Surface #1"

    service.set_env({"BAGLEY_NAME": None}, path)
    assert "BAGLEY_NAME" not in service.read_env(path)
    for bad in ({"1BAD": "x"}, {"BAGLEY_X": "two\nlines"}, {"BAGLEY_X": "it's"}):
        with pytest.raises(ValueError):
            service.set_env(bad, path)


def test_env_show_masks_secrets(home, capsys):
    assert cli.main(["env", "set", "BAGLEY_TOKEN=abcdefghijklmnop", "BAGLEY_PORT=8765"]) == 0
    printed = capsys.readouterr().out
    assert "abcdefghijklmnop" not in printed and "****mnop" in printed
    assert cli.main(["env", "show"]) == 0
    printed = capsys.readouterr().out
    assert "****mnop" in printed and "8765" in printed and "abcdefgh" not in printed
    assert cli.main(["env", "show", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["values"] == {"BAGLEY_TOKEN": "****mnop", "BAGLEY_PORT": "8765"}
    assert service.masked("BAGLEY_API_KEY", "short") == "****"
    assert service.masked("BAGLEY_PUBLIC_URL", "https://x") == "https://x"
    assert cli.main(["env", "set", "NOEQUALS"]) == 2


def test_cli_reads_the_env_file(home, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BAGLEY_PERSONA", raising=False)
    service.set_env({"BAGLEY_PERSONA": "concise"})
    monkeypatch.setattr(service, "systemd_available", lambda: False)
    cli.main(["service", "status"])
    assert os.environ.pop("BAGLEY_PERSONA") == "concise"


# Tailscale --------------------------------------------------------------------------------------


def test_parse_tailscale_status():
    run = fake_runner({"status --json": (0, json.dumps(STATUS))})
    net = tailscale.read_status(run)
    assert net.running and net.machine == "surface" and net.login == "me@example.com"
    assert (
        net.dns_name == "surface.tail1234.ts.net" and net.url == "https://surface.tail1234.ts.net"
    )
    assert net.suffix == "tail1234.ts.net" and net.magic_dns and net.ips[0] == "100.101.102.103"
    assert [(p.name, p.os, p.online) for p in net.peers] == [
        ("b1t", "linux", False),
        ("H4CH1", "windows", True),
        ("s25-ultra", "android", True),
    ]
    assert tailscale.serve_command(8765) == [
        "tailscale", "serve", "--bg", "--https=443", "http://127.0.0.1:8765"
    ]  # fmt: skip
    assert tailscale.env_lines(
        net, {"BAGLEY_ALLOWED_HOSTS": "bagley.lan, surface.tail1234.ts.net"}
    ) == {
        "BAGLEY_ALLOWED_HOSTS": "bagley.lan,surface.tail1234.ts.net",
        "BAGLEY_PUBLIC_URL": "https://surface.tail1234.ts.net",
    }
    serve = fake_runner({"serve status": (0, json.dumps(SERVE))})
    assert tailscale.serving(net, serve) == "http://127.0.0.1:8765"

    with pytest.raises(tailscale.TailscaleError, match="not installed"):
        tailscale.read_status(fake_runner({"status --json": (127, "")}))
    with pytest.raises(tailscale.TailscaleError, match="failed"):
        tailscale.read_status(fake_runner({"status --json": (1, "not logged in")}))
    stopped = tailscale.parse_status({"BackendState": "NeedsLogin", "Self": {}})
    assert not stopped.running and stopped.url == ""


def test_tailscale_command_applies_env(home, monkeypatch, capsys):
    monkeypatch.setattr(
        service,
        "run_command",
        fake_runner({"status --json": (0, json.dumps(STATUS)), "serve status": (0, "{}")}),
    )
    assert cli.main(["tailscale"]) == 0
    out = capsys.readouterr().out
    assert "tailscale serve --bg --https=443 http://127.0.0.1:8765" in out
    assert "BAGLEY_ALLOWED_HOSTS=surface.tail1234.ts.net" in out
    assert "BAGLEY_TAILSCALE_USERS=me@example.com" in out
    assert service.read_env() == {}

    assert cli.main(["tailscale", "--apply", "--port", "8800"]) == 0
    assert "http://127.0.0.1:8800" in capsys.readouterr().out
    assert service.read_env() == {
        "BAGLEY_ALLOWED_HOSTS": "surface.tail1234.ts.net",
        "BAGLEY_PUBLIC_URL": "https://surface.tail1234.ts.net",
    }
    assert cli.main(["tailscale", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["env"]["BAGLEY_PUBLIC_URL"].startswith("https://")


def test_find_bagley_on_peers():
    net = tailscale.parse_status(STATUS)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.host == "100.64.0.3":
            return httpx.Response(200, json={"state": "idle", "code": "IDLE"})
        if request.url.host == "s25-ultra.tail1234.ts.net":
            return httpx.Response(401, text="Unauthorized. Open the URL printed by `bagley` (...)")
        raise httpx.ConnectError("refused", request=request)

    found = asyncio.run(
        tailscale.find_bagleys(net.peers, 8765, transport=httpx.MockTransport(handler))
    )
    assert found == {
        "H4CH1": "http://100.64.0.3:8765",
        "s25-ultra": "https://s25-ultra.tail1234.ts.net",
    }
    assert not any("100.64.0.2" in url for url in asked)  # Offline peers aren't probed.


def test_tailscale_endpoint(make_runtime, monkeypatch):
    monkeypatch.setattr(
        service,
        "run_command",
        fake_runner(
            {"status --json": (0, json.dumps(STATUS)), "serve status": (0, json.dumps(SERVE))}
        ),
    )
    rt = make_runtime()
    rt.config.allowed_hosts = ["surface.tail1234.ts.net"]
    with TestClient(create_app(rt), base_url="http://localhost") as client:
        body = client.get("/api/tailscale").json()
    assert body["available"] and body["url"] == "https://surface.tail1234.ts.net"
    assert body["serving"] == "http://127.0.0.1:8765" and body["host_allowed"]
    assert body["public_url_set"] is False and body["identity_check"] is False


# Tailscale identity check -----------------------------------------------------------------------


def guarded(make_runtime, **config) -> TestClient:
    rt = make_runtime()
    rt.config.allowed_hosts = ["surface.tail1234.ts.net"]
    for key, value in config.items():
        setattr(rt.config, key, value)
    return TestClient(create_app(rt), base_url="http://localhost")


def test_config_reads_tailscale_users():
    config = ServerConfig.from_env({"BAGLEY_TAILSCALE_USERS": " Me@Example.com, other@x.io ,"})
    assert config.tailscale_users == ["me@example.com", "other@x.io"]
    assert ServerConfig.from_env({}).tailscale_users == []


def test_identity_check_without_token(make_runtime):
    tailnet = {"Host": "surface.tail1234.ts.net"}
    with guarded(make_runtime, tailscale_users=["me@example.com"]) as client:
        assert client.get("/api/activity").status_code == 200  # Loopback name: not checked.
        denied = client.get("/api/activity", headers=tailnet)
        assert denied.status_code == 403 and "tailnet user" in denied.text
        me = {**tailnet, "Tailscale-User-Login": "Me@Example.com"}
        assert client.get("/api/activity", headers=me).status_code == 200
        stranger = {**tailnet, "Tailscale-User-Login": "eve@example.com"}
        assert client.get("/api/activity", headers=stranger).status_code == 403
    with guarded(make_runtime, tailscale_users=[]) as client:  # Off: no header needed.
        assert client.get("/api/activity", headers=tailnet).status_code == 200


def test_identity_check_with_token(make_runtime):
    tailnet = {"Host": "surface.tail1234.ts.net"}
    stranger = {**tailnet, "Tailscale-User-Login": "eve@example.com"}
    me = {**tailnet, "Tailscale-User-Login": "me@example.com"}
    with guarded(make_runtime, tailscale_users=["me@example.com"], token="tok") as client:
        assert client.get("/api/activity", headers=stranger).status_code == 403
        bearer = {**stranger, "Authorization": "Bearer tok"}
        assert client.get("/api/activity", headers=bearer).status_code == 200  # Token wins.
        wrong = {**stranger, "Authorization": "Bearer nope"}
        assert client.get("/api/activity", headers=wrong).status_code == 403
        assert client.get("/api/activity", headers=me).status_code == 401  # Token still needed.
        link = client.get("/?token=tok", headers=stranger, follow_redirects=False)
        assert link.status_code == 303 and "bagley_token=tok" in link.headers["set-cookie"]


# Notifications ----------------------------------------------------------------------------------


def test_notify_test_endpoint(make_runtime, monkeypatch):
    sent: list[httpx.Request] = []

    def ntfy(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "x"})

    from bagley import notify

    monkeypatch.setattr(notify, "desktop_available", lambda: False)
    rt = make_runtime(ntfy_url="https://ntfy.example/bagley-abc", ntfy_level="important")
    rt.http = httpx.AsyncClient(transport=httpx.MockTransport(ntfy))
    with TestClient(create_app(rt), base_url="http://localhost") as client:
        body = client.post("/api/notify/test", json={"targets": ["phone"]}).json()
        assert body["level"] == "important"
        assert body["results"] == {"phone": {"ok": True, "detail": "Sent to ntfy as important."}}
        assert sent[0].headers["Title"] == "BAGLEY // LINK TEST"
        assert sent[0].headers["Priority"] == "high" and sent[0].headers["Click"].endswith("/")

        body = client.post("/api/notify/test", json={}).json()
        assert body["results"]["desktop"] == {
            "ok": False,
            "detail": "notify-send is not available here.",
        }
        assert body["results"]["phone"]["ok"] and len(sent) == 2
        assert client.post("/api/notify/test", json={"targets": ["pager"]}).status_code == 422

        rt.update_preferences({"ntfy_url": ""})
        body = client.post("/api/notify/test", json={"targets": ["phone"]}).json()
        assert body["results"]["phone"]["ok"] is False and len(sent) == 2
