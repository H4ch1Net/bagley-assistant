from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bagley.automations import ScheduleError
from bagley.server import create_app
from bagley.tools import ToolError
from bagley.watchdog.baseline import NEW_FOR, Baseline, reset
from bagley.watchdog.collectors import (
    COLLECTORS,
    SSH_JOURNAL,
    Context,
    Finding,
    Section,
    auth_log_entries,
    clean,
    debsecan_suite,
    run_command,
)
from bagley.watchdog.report import Report, collect, section_ids
from tests.mock_llm import Reply

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 8, 7, 30).timestamp()
SYSTEMCTL = ("systemctl", "--failed", "--no-legend", "--plain", "--no-pager")
USER_SYSTEMCTL = ("systemctl", "--user", "--failed", "--no-legend", "--plain", "--no-pager")
JOURNAL = ("journalctl", "-p", "err", "-b", "--since", "24 hours ago", "-o", "json",
           "--no-pager", "-n", "500")  # fmt: skip
SS = ("ss", "-H", "-tulpn")
NEIGH = ("ip", "-j", "neigh")
ROUTE = ("ip", "-j", "route", "show", "default")
TAILSCALE = ("tailscale", "status", "--json")
APT = ("apt", "list", "--upgradable")
DEBSECAN = ("debsecan", "--suite", "sid", "--only-fixed")
SSH = tuple(SSH_JOURNAL)
KALI = 'PRETTY_NAME="Kali GNU/Linux Rolling"\nNAME="Kali GNU/Linux"\nID=kali\nVERSION_CODENAME=kali-rolling\nID_LIKE=debian\n'
APT_OUT = """\
Listing...
linux-image-amd64/kali-rolling 6.11.2-1kali1 amd64 [upgradable from: 6.11.1-1kali1]
openssl/kali-rolling 3.3.2-1 amd64 [upgradable from: 3.3.1-1]
vim/kali-rolling 2:9.1.0-1 amd64 [upgradable from: 2:9.1.0-0]
burpsuite/kali-rolling 2024.9-0kali1 amd64 [upgradable from: 2024.8-0kali1]
"""
DEBSECAN_OUT = """\
CVE-2024-0001 openssl (fixed, remotely exploitable, high urgency)
CVE-2024-0002 openssl (fixed, low urgency)
CVE-2024-0100 libtiff6 (fixed, medium urgency)
"""


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeSystem:
    """Canned output per command line. Programs not in ``programs`` are missing."""

    def __init__(self, outputs: dict, programs: set[str] | None = None) -> None:
        self.outputs = {tuple(k): v for k, v in outputs.items()}
        self.programs = programs if programs is not None else {k[0] for k in self.outputs}
        self.calls: list[list[str]] = []

    def set(self, args: tuple[str, ...] | list[str], value) -> None:
        self.outputs[tuple(args)] = value

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.programs else None

    async def run(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        assert isinstance(args, list) and timeout > 0
        self.calls.append(list(args))
        if args[0] not in self.programs:
            return 127, "", f"{args[0]}: command not found"
        value = self.outputs.get(tuple(args))
        if value is None:
            return 1, "", f"unexpected call: {' '.join(args)}"
        return (0, value, "") if isinstance(value, str) else value


class FakePsutil:
    def __init__(self, partitions=(), usage=None, connections=None) -> None:
        self.partitions = list(partitions)
        self.usage = usage or {}
        self.connections = connections

    def disk_partitions(self, all: bool = False):
        return self.partitions

    def disk_usage(self, mount: str):
        return self.usage[mount]

    def net_connections(self, kind: str = "inet"):
        if isinstance(self.connections, Exception):
            raise self.connections
        return self.connections or []

    def Process(self, pid: int):
        return SimpleNamespace(name=lambda: f"proc{pid}")


def part(device: str, mount: str, fstype: str) -> SimpleNamespace:
    return SimpleNamespace(device=device, mountpoint=mount, fstype=fstype, opts="rw")


def usage(total_gb: float, percent: float) -> SimpleNamespace:
    total = int(total_gb * 1024**3)
    used = int(total * percent / 100)
    return SimpleNamespace(total=total, used=used, free=total - used, percent=percent)


def journal_line(message: str, ts: float = NOW - 3600, **fields) -> str:
    entry = {"MESSAGE": message, "__REALTIME_TIMESTAMP": str(int(ts * 1e6)), "PRIORITY": "3"}
    return json.dumps({**entry, **fields})


def ssh_journal() -> str:
    lines = []
    for i in range(30):  # A brute force from the internet.
        lines.append(
            journal_line(f"Failed password for root from 45.155.205.9 port {50000 + i} ssh2")
        )
    # One attempt by an unknown user shows up three times; it counts once.
    lines.append(journal_line("Invalid user admin from 45.155.205.9 port 51000"))
    lines.append(
        journal_line("Failed password for invalid user admin from 45.155.205.9 port 51000 ssh2")
    )
    lines.append(
        journal_line(
            "pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 tty=ssh "
            "ruser= rhost=45.155.205.9  user=root"
        )
    )
    lines.append(journal_line("Failed password for git from 192.168.1.20 port 40000 ssh2"))
    lines.append(journal_line("Accepted publickey for h4ch1 from 100.64.0.2 port 50123 ssh2"))
    lines.append(
        journal_line("Failed password for root from 185.220.101.7 port 3999 ssh2", NOW - 600)
    )
    lines.append(
        journal_line("Accepted password for root from 185.220.101.7 port 4000 ssh2", NOW - 500)
    )
    return "\n".join(lines) + "\n"


NEIGHBORS = [
    {"dst": "192.168.1.1", "dev": "wlan0", "lladdr": "aa:bb:cc:00:00:01", "state": ["REACHABLE"]},
    {"dst": "fe80::1", "dev": "wlan0", "lladdr": "aa:bb:cc:00:00:01", "router": None, "state": ["STALE"]},
    {"dst": "192.168.1.20", "dev": "wlan0", "lladdr": "aa:bb:cc:00:00:20", "state": ["STALE"]},
    {"dst": "192.168.1.99", "dev": "wlan0", "state": ["FAILED"]},
    {"dst": "192.168.1.98", "dev": "wlan0", "lladdr": "aa:bb:cc:00:00:98", "state": ["INCOMPLETE"]},
    {"dst": "224.0.0.251", "dev": "wlan0", "lladdr": "01:00:5e:00:00:fb", "state": ["NOARP"]},
]  # fmt: skip
PEERS = {
    "BackendState": "Running",
    "Self": {"HostName": "h4ch1"},
    "Peer": {
        "nodekey:1": {"ID": "n1", "HostName": "pixel", "TailscaleIPs": ["100.64.0.2"], "OS": "android", "Online": True},
    },
}  # fmt: skip
SS_OUT = """\
tcp   LISTEN 0      128          0.0.0.0:22        0.0.0.0:*    users:(("sshd",pid=612,fd=3))
tcp   LISTEN 0      128             [::]:22           [::]:*    users:(("sshd",pid=612,fd=4))
tcp   LISTEN 0      4096       127.0.0.1:8765      0.0.0.0:*    users:(("python",pid=900,fd=6))
tcp   LISTEN 0      4096       127.0.0.1:45678     0.0.0.0:*
udp   UNCONN 0      0            0.0.0.0:5353      0.0.0.0:*
udp   UNCONN 0      0            0.0.0.0:41641     0.0.0.0:*
tcp   ESTAB  0      0      192.168.1.10:51234 140.82.112.3:443
"""


def kali_laptop(
    tmp_path: Path, clock: Clock | None = None, store=None
) -> tuple[FakeSystem, Context]:
    """A Kali laptop with something to report in every section."""
    failed = [
        {"unit": "nftables.service", "load": "loaded", "active": "failed", "sub": "failed", "description": "Netfilter Tables"},
        {"unit": "bluetooth.service", "load": "loaded", "active": "failed", "sub": "failed", "description": "Bluetooth service"},
    ]  # fmt: skip
    errors = "\n".join(
        [
            journal_line("Failed to set mode", _SYSTEMD_UNIT="bluetooth.service"),
            journal_line("Failed to set mode", _SYSTEMD_UNIT="bluetooth.service"),
            journal_line("Failed to set mode", _SYSTEMD_UNIT="bluetooth.service"),
            journal_line("", MESSAGE=[65, 67, 80, 73, 255], _TRANSPORT="kernel", PRIORITY="2"),
        ]
    )
    fake = FakeSystem(
        {
            (*SYSTEMCTL, "--output=json"): json.dumps(failed),
            (*USER_SYSTEMCTL, "--output=json"): (1, "", "Unknown output 'json'."),
            USER_SYSTEMCTL: "● pipewire.service loaded failed failed PipeWire Multimedia Service\n",
            JOURNAL: errors,
            ("smartctl", "--scan", "-j"): json.dumps(
                {"devices": [{"name": "/dev/nvme0"}, {"name": "/dev/sda"}]}
            ),
            ("smartctl", "-H", "-j", "/dev/nvme0"): (
                2,
                json.dumps({"smartctl": {"messages": [{"string": "Permission denied"}]}}),
                "",
            ),
            ("smartctl", "-H", "-j", "/dev/sda"): (
                8,
                json.dumps({"smart_status": {"passed": False}}),
                "",
            ),
            APT: APT_OUT,
            NEIGH: json.dumps(NEIGHBORS),
            ROUTE: json.dumps([{"dst": "default", "gateway": "192.168.1.1", "dev": "wlan0"}]),
            (
                "nmcli",
                "-t",
                "-f",
                "NAME,DEVICE",
                "connection",
                "show",
                "--active",
            ): "HomeNet:wlan0\n",
            TAILSCALE: json.dumps(PEERS),
            SS: SS_OUT,
            SSH: ssh_journal(),
            ("systemctl", "is-active", "sshd", "ssh", "sshd.socket", "ssh.socket"): (
                3,
                "active\ninactive\ninactive\ninactive\n",
                "",
            ),
            DEBSECAN: DEBSECAN_OUT,
        }
    )
    (tmp_path / "os-release").write_text(KALI)
    lists = tmp_path / "apt-lists"
    lists.mkdir(exist_ok=True)
    (lists / "http.kali.org_kali_dists_kali-rolling_InRelease").touch()
    stamp = (clock or Clock()).now - 3600  # Updated an hour ago.
    os.utime(lists / "http.kali.org_kali_dists_kali-rolling_InRelease", (stamp, stamp))
    battery = tmp_path / "sys" / "class" / "power_supply" / "BAT0"
    battery.mkdir(parents=True, exist_ok=True)
    for name, value in {
        "energy_full": "40000000",
        "energy_full_design": "57000000",
        "cycle_count": "412",
        "status": "Discharging",
        "capacity": "64",
    }.items():
        (battery / name).write_text(value + "\n")
    (tmp_path / "sys" / "class" / "power_supply" / "AC").mkdir(exist_ok=True)
    ps = FakePsutil(
        partitions=[
            part("/dev/nvme0n1p2", "/", "btrfs"),
            part("/dev/nvme0n1p2", "/home", "btrfs"),
            part("/dev/nvme0n1p1", "/boot", "vfat"),
            part("tmpfs", "/tmp", "tmpfs"),
            part("/dev/sda1", "/mnt/data", "ext4"),
        ],
        usage={"/": usage(500, 50), "/home": usage(500, 50), "/boot": usage(1, 96.2), "/mnt/data": usage(2000, 88)},
    )  # fmt: skip
    ctx = Context(
        run=fake.run,
        which=fake.which,
        clock=clock or Clock(),
        baseline=Baseline(store) if store is not None else None,
        platform="linux",
        sysfs=tmp_path / "sys",
        psutil=ps,
        os_release=tmp_path / "os-release",
        apt_lists=tmp_path / "apt-lists",
        auth_log=tmp_path / "auth.log",
    )
    return fake, ctx


def by_id(report: Report) -> dict[str, Section]:
    return {s.id: s for s in report.sections}


def texts(section: Section) -> list[str]:
    return [f.text for f in section.findings]


@pytest.fixture
def store(make_runtime):
    return make_runtime().store


# Collectors --------------------------------------------------------------------------------------


async def test_full_report_on_a_kali_laptop(tmp_path, store):
    _, ctx = kali_laptop(tmp_path, store=store)
    report = await collect(None, context=ctx)
    s = by_id(report)
    assert [x.id for x in report.sections] == list(COLLECTORS)

    assert s["services"].status == "crit"  # The firewall failed.
    assert texts(s["services"]) == [
        "nftables.service FAILED",
        "bluetooth.service FAILED",
        "pipewire.service (user) FAILED",
    ]
    assert [f.severity for f in s["services"].findings] == ["crit", "warn", "warn"]
    assert s["services"].summary.startswith("3 FAILED UNITS // nftables.service")

    assert s["journal"].status == "warn"  # A kernel message at priority crit.
    assert s["journal"].summary == "4 JOURNAL ERRORS // 2 SOURCES"
    assert texts(s["journal"]) == ["bluetooth.service ×3", "kernel ×1"]
    assert s["journal"].findings[1].detail == "ACPI�"  # Non-UTF-8 bytes are decoded.

    disks = s["disks"]
    assert disks.status == "crit"
    assert texts(disks) == ["/boot 96%", "/mnt/data 88%", "SMART FAILING /dev/sda"]
    assert disks.summary == "SMART FAILURE // /boot 96% · /mnt/data 88%"
    assert [f["mount"] for f in disks.data["filesystems"]] == ["/", "/boot", "/mnt/data"]
    assert disks.data["filesystems"][0]["also"] == ["/home"]  # One btrfs device.
    assert disks.data["smart"] == {"/dev/nvme0": "NO ACCESS", "/dev/sda": "FAILED"}

    assert s["battery"].status == "warn"
    assert s["battery"].summary == "BAT0 HEALTH 70% // 412 CYCLES // 64% DISCHARGING"
    assert s["battery"].findings[0].detail == "40.0 Wh of 57.0 Wh when new, 412 cycles"

    updates = s["updates"]
    assert updates.status == "warn"  # Kali has no -security suite: names tell.
    assert updates.summary == "2 SECURITY UPDATES // 4 UPDATES PENDING"
    assert texts(updates) == [
        "linux-image-amd64 6.11.1-1kali1 -> 6.11.2-1kali1",
        "openssl 3.3.1-1 -> 3.3.2-1",
        "2 OTHER UPDATES",
    ]
    assert updates.findings[2].detail == "vim, burpsuite"
    assert updates.data["lists_age"] == 3600

    network = s["network"]
    assert network.status == "info"
    assert network.summary == "BASELINE RECORDED // 2 DEVICES ON HomeNet // 1 TAILNET PEER"
    assert network.data["network"]["id"] == "aa:bb:cc:00:00:01"  # The gateway's MAC.
    assert [d["ip"] for d in network.data["devices"]] == ["192.168.1.1", "192.168.1.20"]

    ports = s["ports"]
    assert ports.status == "info"
    assert ports.summary == "BASELINE RECORDED // 4 LISTENING // 2 SENSITIVE EXPOSED"
    assert [x["key"] for x in ports.data["sockets"]] == [
        "tcp 0.0.0.0:22",
        "tcp [::]:22",
        "udp 0.0.0.0:5353",
        "tcp 127.0.0.1:8765",
    ]
    assert texts(ports) == ["EXPOSED TCP 0.0.0.0:22 sshd", "EXPOSED TCP [::]:22 sshd"]

    ssh = s["ssh"]
    assert ssh.status == "crit"
    assert (
        ssh.summary == "1 LOGIN AFTER FAILURES // 33 FAILED SSH LOGINS // 3 SOURCES // SSHD ACTIVE"
    )
    assert ssh.data["sources"][0] == {
        "ip": "45.155.205.9",
        "count": 31,
        "external": True,
        "users": ["root", "admin"],
    }
    assert "ACCEPTED root@185.220.101.7 AFTER 1 FAILURES" in texts(ssh)
    assert "ACCEPTED h4ch1@100.64.0.2" in texts(ssh)
    local = next(f for f in ssh.findings if f.text.startswith("192.168.1.20"))
    assert local.severity == "info"  # From the local network.

    vulns = s["vulns"]
    assert vulns.status == "crit" and vulns.data["source"] == "debsecan"
    assert vulns.summary == "2 VULNERABLE PACKAGES // 1 HIGH // 1 UPGRADABLE NOW"
    assert texts(vulns) == ["openssl // CVE-2024-0001 +1", "libtiff6 // CVE-2024-0100"]
    assert vulns.findings[0].detail == (
        "HIGH URGENCY // remotely exploitable // upgrade available: sudo apt full-upgrade"
    )
    assert vulns.findings[1].detail == "MEDIUM URGENCY // fixed in Debian sid, not in Kali yet"

    assert report.status() == "crit" and report.level() == "critical"
    assert report.counts() == {"ok": 0, "info": 2, "warn": 3, "crit": 4, "unavailable": 0}
    assert report.headline() == (
        "4 CRIT · 3 WARN // 3 FAILED UNITS · SMART FAILURE · 1 LOGIN AFTER FAILURES"
    )


async def test_missing_programs_and_other_platforms(tmp_path):
    fake = FakeSystem({}, programs=set())
    ctx = Context(
        run=fake.run,
        which=fake.which,
        clock=Clock(),
        platform="linux",
        sysfs=tmp_path,
        psutil=FakePsutil(connections=PermissionError("Access denied")),
        os_release=tmp_path / "none",
        apt_lists=tmp_path / "none",
        auth_log=tmp_path / "none",
    )
    report = await collect(None, context=ctx)
    assert {s.id: s.summary for s in report.sections} == {
        "services": "NO SYSTEMD ON THIS SYSTEM",
        "journal": "NO SYSTEMD JOURNAL ON THIS SYSTEM",
        "disks": "NO FILESYSTEMS FOUND",
        "battery": "NO BATTERY FOUND",
        "updates": "NEEDS APT (KALI, DEBIAN, UBUNTU)",
        "network": "IP AND TAILSCALE NOT FOUND",
        "ports": "SOCKETS UNREADABLE: Access denied",
        "ssh": "NO SYSTEMD JOURNAL ON THIS SYSTEM",
        "vulns": "NEEDS KALI OR DEBIAN (debsecan)",
    }
    assert all(s.status == "unavailable" for s in report.sections)
    assert report.level() == "info" and report.headline() == "ALL CLEAR // 0 OK · 9 N/A"
    assert fake.calls == []

    conn = SimpleNamespace(
        type=1, status="LISTEN", laddr=SimpleNamespace(ip="0.0.0.0", port=5900), raddr=(), pid=42
    )
    ctx.platform, ctx.psutil = "darwin", FakePsutil(connections=[conn])  # fmt: skip
    report = await collect(None, ["battery", "updates", "ports"], context=ctx)
    assert [s.summary for s in report.sections] == [
        "BATTERY HEALTH NEEDS LINUX",
        "PACKAGE UPDATES NEED LINUX",
        "1 LISTENING // 1 SENSITIVE EXPOSED",
    ]
    assert report.sections[2].findings[0].text == "EXPOSED TCP 0.0.0.0:5900 proc42"


async def test_permission_problems_degrade_quietly(tmp_path, store):
    fake, ctx = kali_laptop(tmp_path, store=store)
    hint = (
        "Hint: You are currently not seeing messages from other users and the system.\n"
        "      Users in groups 'adm', 'systemd-journal', 'wheel' can see all messages.\n"
    )
    fake.set(JOURNAL, (0, journal_line("oops", _SYSTEMD_USER_UNIT="app.service"), hint))
    fake.set(SSH, (0, "", hint))
    fake.set(("smartctl", "-H", "-j", "/dev/sda"), (2, "", "Permission denied"))
    fake.set((*USER_SYSTEMCTL, "--output=json"), (1, "", "Failed to connect to bus"))
    fake.set(USER_SYSTEMCTL, (1, "", "Failed to connect to bus"))
    s = by_id(await collect(None, ["services", "journal", "disks", "ssh"], context=ctx))
    assert texts(s["journal"]) == ["app.service ×1", "ONLY YOUR OWN JOURNAL IS READABLE"]
    assert "usermod -aG adm" in s["journal"].findings[-1].detail
    # Without the system journal "no failed logins" would be a lie.
    assert s["ssh"].status == "unavailable" and s["ssh"].summary.endswith("JOIN adm")
    assert s["disks"].data["smart"] == {"/dev/nvme0": "NO ACCESS", "/dev/sda": "NO ACCESS"}
    assert not any("SMART" in t for t in texts(s["disks"]))
    assert s["services"].data["user"] is None and len(s["services"].findings) == 2

    fake.set(JOURNAL, (0, "", "No journal files were found.\n"))
    fake.set(SSH, (0, "", "No journal files were found.\n"))
    s = by_id(await collect(None, ["journal", "ssh"], context=ctx))
    assert s["journal"].summary == s["ssh"].summary == "NO JOURNAL FILES FOUND"

    # With JSON output journalctl says nothing when there is no journal: probe for any entry.
    fake.set(JOURNAL, "")
    fake.set(SSH, "")
    fake.set(("journalctl", "-n", "1", "-o", "json", "--no-pager"), "")
    s = by_id(await collect(None, ["journal", "ssh"], context=ctx))
    assert s["journal"].summary == s["ssh"].summary == "NO READABLE JOURNAL ENTRIES"
    fake.set(("journalctl", "-n", "1", "-o", "json", "--no-pager"), journal_line("boot"))
    s = by_id(await collect(None, ["journal", "ssh"], context=ctx))
    assert s["journal"].status == "ok" and s["journal"].summary == "NO ERRORS // 24H"
    assert s["ssh"].status == "ok" and s["ssh"].summary == "NO FAILED LOGINS // SSHD ACTIVE"


async def test_older_systemctl_and_journal_timeouts(tmp_path, store):
    fake, ctx = kali_laptop(tmp_path, store=store)
    fake.set((*SYSTEMCTL, "--output=json"), (1, "", "Unknown output 'json'."))
    fake.set(SYSTEMCTL, "  nginx.service loaded failed failed A high performance web server\n")
    fake.set(JOURNAL, (124, "", "journalctl timed out after 20s"))
    s = by_id(await collect(None, ["services", "journal"], context=ctx))
    assert texts(s["services"])[0] == "nginx.service FAILED"
    assert s["services"].findings[0].detail == "A high performance web server"
    assert s["journal"].summary == "JOURNALCTL TIMED OUT"


async def test_a_collector_that_hangs_or_breaks_is_contained(tmp_path, monkeypatch):
    _, ctx = kali_laptop(tmp_path)

    async def hang(ctx):
        import asyncio

        await asyncio.sleep(10)

    async def boom(ctx):
        raise RuntimeError("kaput")

    monkeypatch.setattr(COLLECTORS["battery"], "func", hang)
    monkeypatch.setattr(COLLECTORS["battery"], "timeout", 0.05)
    monkeypatch.setattr(COLLECTORS["updates"], "func", boom)
    s = by_id(await collect(None, ["battery", "updates"], context=ctx))
    assert s["battery"].summary == "TIMED OUT AFTER 0.05S"
    assert s["updates"].summary == "ERROR: kaput"
    assert ctx.running == {}


async def test_reports_asked_together_share_slow_checks(tmp_path):
    import asyncio

    fake, ctx = kali_laptop(tmp_path)
    slow = fake.run

    async def run(args, timeout):
        await asyncio.sleep(0.05)
        return await slow(args, timeout)

    ctx.run = run
    first, second = await asyncio.gather(
        collect(None, ["updates"], context=ctx), collect(None, ["updates", "battery"], context=ctx)
    )
    assert fake.calls.count(list(APT)) == 1
    assert by_id(first)["updates"] is by_id(second)["updates"]
    await collect(None, ["updates"], context=ctx)
    assert fake.calls.count(list(APT)) == 2  # Finished runs aren't reused.


# Updates -----------------------------------------------------------------------------------------


async def test_updates_on_debian_and_with_stale_lists(tmp_path):
    fake, ctx = kali_laptop(tmp_path)
    fake.set(
        APT,
        "Listing...\n"
        "libssl3t64/trixie-security 3.5.1-1+deb13u1 amd64 [upgradable from: 3.5.1-1]\n"
        "htop/trixie 3.4.1-5 amd64 [upgradable from: 3.4.1-4]\n",
    )
    s = by_id(await collect(None, ["updates"], context=ctx))["updates"]
    assert s.summary == "1 SECURITY UPDATE // 2 UPDATES PENDING"
    assert texts(s) == ["libssl3t64 3.5.1-1 -> 3.5.1-1+deb13u1", "1 OTHER UPDATE"]

    ctx.clock.now += 9 * 86400  # Nobody ran apt update for over a week.
    fake.set(APT, "Listing...\n")
    s = by_id(await collect(None, ["updates"], context=ctx))["updates"]
    assert s.status == "warn" and s.summary == "UP TO DATE // LISTS 9D OLD"
    assert texts(s) == ["PACKAGE LISTS 9 DAYS OLD"]
    assert "sudo apt update" in s.findings[0].detail

    fake.set(APT, (100, "", "E: Could not open lock file"))
    s = by_id(await collect(None, ["updates"], context=ctx))["updates"]
    assert s.status == "unavailable" and s.summary.startswith("APT FAILED")


# Baselines ---------------------------------------------------------------------------------------


async def test_baseline_flags_new_ports_devices_and_peers(tmp_path, store):
    clock = Clock()
    fake, ctx = kali_laptop(tmp_path, clock, store)
    first = by_id(await collect(None, ["network", "ports"], context=ctx))
    assert first["network"].summary.startswith("BASELINE RECORDED")
    assert first["ports"].summary.startswith("BASELINE RECORDED")

    clock.now += 3600
    again = by_id(await collect(None, ["network", "ports"], context=ctx))
    assert again["network"].status == "ok" and again["network"].findings == []
    assert again["ports"].status == "info"  # Only the known exposed sshd.

    fake.set(
        SS,
        SS_OUT
        + 'tcp LISTEN 0 5 0.0.0.0:5900 0.0.0.0:* users:(("wayvnc",pid=77,fd=5))\n'
        + 'tcp LISTEN 0 5 192.168.1.10:8080 0.0.0.0:* users:(("node",pid=78,fd=5))\n'
        + 'tcp LISTEN 0 5 127.0.0.1:5432 0.0.0.0:* users:(("postgres",pid=79,fd=5))\n',
    )
    fake.set(
        NEIGH,
        json.dumps(
            [*NEIGHBORS, {"dst": "192.168.1.66", "dev": "wlan0", "lladdr": "de:ad:be:ef:00:66", "state": ["REACHABLE"]}]
        ),
    )  # fmt: skip
    peers = json.loads(json.dumps(PEERS))
    peers["Peer"]["nodekey:2"] = {
        "ID": "n2",
        "HostName": "kali",
        "TailscaleIPs": ["100.64.0.9"],
        "OS": "linux",
    }
    fake.set(TAILSCALE, json.dumps(peers))
    clock.now += 3600
    report = await collect(None, ["network", "ports"], context=ctx)
    s = by_id(report)
    ports = s["ports"]
    assert ports.status == "crit"
    assert [(f.severity, f.text) for f in ports.findings[:3]] == [
        ("crit", "NEW TCP 0.0.0.0:5900 wayvnc"),  # VNC on every interface.
        ("warn", "NEW TCP 192.168.1.10:8080 node"),
        ("info", "NEW TCP 127.0.0.1:5432 postgres"),  # Reachable from this computer only.
    ]
    assert ports.findings[0].detail == "vnc on every interface"
    assert (
        ports.summary == "3 NEW PORTS 0.0.0.0:5900 wayvnc +2 // 7 LISTENING // 3 SENSITIVE EXPOSED"
    )
    network = s["network"]
    assert network.status == "warn"
    assert texts(network) == [
        "NEW DEVICE 192.168.1.66 de:ad:be:ef:00:66 (REACHABLE)",
        "NEW TAILNET PEER kali 100.64.0.9 (linux)",
    ]
    assert network.summary.startswith("1 NEW DEVICE · 1 NEW TAILNET PEER // 3 DEVICES ON HomeNet")
    assert report.headline() == (
        "1 CRIT · 1 WARN // 3 NEW PORTS 0.0.0.0:5900 wayvnc +2 · 1 NEW DEVICE · 1 NEW TAILNET PEER"
    )

    # New items stay flagged for a while, so the next briefing still shows them...
    clock.now += NEW_FOR - 7200
    assert by_id(await collect(None, ["ports"], context=ctx))["ports"].status == "crit"
    # ...then they are part of the baseline.
    clock.now += 7200
    later = by_id(await collect(None, ["network", "ports"], context=ctx))
    assert later["network"].status == "ok" and later["ports"].status == "info"

    removed = reset(store, ["ports"])
    assert removed == ["ports"]
    after = by_id(await collect(None, ["network", "ports"], context=ctx))
    assert after["ports"].summary.startswith("BASELINE RECORDED // 7 LISTENING")
    assert after["network"].status == "ok"  # Not reset.


async def test_each_network_gets_its_own_baseline(tmp_path, store):
    fake, ctx = kali_laptop(tmp_path, store=store)
    await collect(None, ["network"], context=ctx)
    fake.set(
        NEIGH,
        json.dumps([{"dst": "10.0.0.1", "dev": "wlan0", "lladdr": "02:22:33:44:55:66", "state": ["REACHABLE"]}]),
    )  # fmt: skip
    fake.set(ROUTE, json.dumps([{"dst": "default", "gateway": "10.0.0.1", "dev": "wlan0"}]))
    s = by_id(await collect(None, ["network"], context=ctx))["network"]
    assert s.status == "info" and s.summary.startswith("BASELINE RECORDED // 1 DEVICE")
    keys = sorted(r["key"] for r in Baseline(store).keys("network.devices."))
    assert keys == ["network.devices.02:22:33:44:55:66", "network.devices.aa:bb:cc:00:00:01"]


def test_baseline_reset_keeps_caches(store):
    base = Baseline(store)
    base.set("ports", {"items": {}})
    base.set("network.devices.x", {"items": {}})
    base.set("network.tailscale", {"items": {}})
    base.set("cache.last_report", {"sections": []})
    assert sorted(base.reset(["network"])) == ["network.devices.x", "network.tailscale"]
    assert base.reset() == ["ports"]
    assert base.get("cache.last_report") == {"sections": []}


# Vulnerabilities ---------------------------------------------------------------------------------


def test_debsecan_suite_follows_the_distribution():
    assert debsecan_suite({"ID": "kali", "VERSION_CODENAME": "kali-rolling"}) == "sid"
    assert debsecan_suite({"ID": "debian", "VERSION_CODENAME": "trixie"}) == "trixie"
    assert debsecan_suite({"ID": "ubuntu", "ID_LIKE": "debian"}) is None


async def test_debsecan_on_debian_and_when_it_cant_answer(tmp_path):
    fake, ctx = kali_laptop(tmp_path)
    (tmp_path / "os-release").write_text("ID=debian\nVERSION_CODENAME=trixie\n")
    fake.set(
        ("debsecan", "--suite", "trixie", "--only-fixed"), "CVE-2024-9 curl (fixed, low urgency)\n"
    )
    s = by_id(await collect(None, ["vulns"], context=ctx))["vulns"]
    assert s.status == "info" and s.data["suite"] == "trixie"
    assert (
        s.findings[0].detail
        == "LOW URGENCY // fixed in Debian trixie, not in your repositories yet"
    )

    fake.set(("debsecan", "--suite", "trixie", "--only-fixed"), "")
    fake.calls.clear()
    s = by_id(await collect(None, ["vulns"], context=ctx))["vulns"]
    assert s.status == "ok" and s.summary == "NO FIXED VULNERABILITIES PENDING"
    assert list(APT) not in fake.calls  # Nothing to look up.

    (tmp_path / "os-release").write_text(KALI)
    fake.set(DEBSECAN, (1, "", "debsecan: error: could not download vulnerability data"))
    s = by_id(await collect(None, ["vulns"], context=ctx))["vulns"]
    assert s.status == "unavailable" and s.summary.startswith("DEBSECAN FAILED")

    fake.programs.discard("debsecan")
    s = by_id(await collect(None, ["vulns"], context=ctx))["vulns"]
    assert s.summary == "INSTALL debsecan // sudo apt install debsecan"


# SSH from auth.log -------------------------------------------------------------------------------


def test_auth_log_lines_in_both_formats():
    stamp = datetime.fromtimestamp(NOW - 1800).astimezone()
    iso = stamp.isoformat(timespec="microseconds")
    classic = stamp.strftime("%b %e %H:%M:%S")
    old = datetime.fromtimestamp(NOW - 3 * 86400).astimezone().isoformat()
    text = "\n".join(
        [
            f"{iso} kali sshd-session[811]: Failed password for root from 45.155.205.9 port 4242 ssh2",
            f"{classic} kali sshd[812]: Invalid user oracle from 45.155.205.9 port 4243",
            f"{iso} kali sudo: kali : TTY=pts/0 ; PWD=/home/kali ; COMMAND=/usr/bin/apt",
            f"{old} kali sshd[700]: Failed password for root from 1.2.3.4 port 1 ssh2",
            "garbage line",
        ]
    )
    entries = auth_log_entries(text, NOW)
    assert [e["MESSAGE"] for e in entries] == [
        "Failed password for root from 45.155.205.9 port 4242 ssh2",
        "Invalid user oracle from 45.155.205.9 port 4243",
    ]
    assert abs(int(entries[0]["__REALTIME_TIMESTAMP"]) / 1e6 - (NOW - 1800)) < 1


async def test_ssh_falls_back_to_auth_log(tmp_path):
    fake, ctx = kali_laptop(tmp_path)
    hint = "Hint: You are currently not seeing messages from other users and the system.\n"
    fake.set(SSH, (0, "", hint))
    stamp = datetime.fromtimestamp(NOW - 600).astimezone().isoformat(timespec="microseconds")
    (tmp_path / "auth.log").write_text(
        "".join(
            f"{stamp} kali sshd-session[9{i}]: Failed password for kali from 45.155.205.9 port {40000 + i} ssh2\n"
            for i in range(3)
        )
    )
    s = by_id(await collect(None, ["ssh"], context=ctx))["ssh"]
    assert s.data["source"] == "auth.log" and s.status == "warn"
    assert s.summary == "3 FAILED SSH LOGINS // 1 SOURCE // SSHD ACTIVE"

    (tmp_path / "auth.log").unlink()
    s = by_id(await collect(None, ["ssh"], context=ctx))["ssh"]
    assert s.status == "unavailable" and s.summary.endswith("JOIN adm")


# Report ------------------------------------------------------------------------------------------


def sample_report() -> Report:
    return Report(
        [
            Section("services", "SERVICES", "ok", "NO FAILED UNITS"),
            Section(
                "disks",
                "DISKS",
                "warn",
                "/home 92%",
                [Finding("warn", "/home 92%", "9.0 GB free of 120 GB (ext4)")],
            ),
            Section(
                "ssh",
                "SSH",
                "crit",
                "41 FAILED SSH LOGINS // 3 SOURCES",
                [Finding("crit", "203.0.113.9 ×30 // root`, [x](http://evil)", "")],
            ),
            Section("battery", "BATTERY", "unavailable", "NO BATTERY FOUND"),
        ],
        created_at=NOW,
        host="H4CH1",
        title="MORNING BRIEFING",
    )


def test_report_formats():
    report = sample_report()
    assert report.title_line() == "MORNING BRIEFING // 081026 // H4CH1"
    assert report.headline() == "1 CRIT · 1 WARN // 41 FAILED SSH LOGINS · /home 92%"
    assert report.level() == "critical" and report.status() == "crit"
    md = report.markdown()
    assert md.splitlines()[0] == "**MORNING BRIEFING // 081026 // H4CH1**"
    assert "[OK]   SERVICES  NO FAILED UNITS" in md
    assert "[CRIT] SSH       41 FAILED SSH LOGINS // 3 SOURCES" in md
    assert "[N/A]  BATTERY   NO BATTERY FOUND" in md
    assert "**[WARN] DISKS**\n- `WARN` `/home 92% — 9.0 GB free of 120 GB (ext4)`" in md
    # Untrusted text stays inside its code span.
    assert "- `CRIT` `203.0.113.9 ×30 // root', [x](http://evil)`" in md
    assert "SERVICES**" not in md  # OK sections have no details.
    text = report.text()
    assert text.splitlines()[0] == "MORNING BRIEFING // 081026 // H4CH1"
    assert "       WARN /home 92%  9.0 GB free of 120 GB (ext4)" in text
    data = report.to_dict()
    assert data["counts"] == {"ok": 1, "info": 0, "warn": 1, "crit": 1, "unavailable": 1}
    assert data["level"] == "critical" and data["headline"] == report.headline()
    assert data["sections"][1]["findings"][0] == {
        "severity": "warn",
        "text": "/home 92%",
        "detail": "9.0 GB free of 120 GB (ext4)",
    }

    report.sections = report.sections[:2]
    assert report.level() == "important" and report.status() == "warn"
    report.sections = [Section("x", "X", "info", "BASELINE RECORDED"), report.sections[0]]
    assert report.level() == "info" and report.status() == "ok"
    assert report.headline() == "ALL CLEAR // 2 OK"


def test_section_ids_and_clean():
    assert section_ids(None) == list(COLLECTORS)
    assert section_ids("ssh, ports") == ["ports", "ssh"]  # Usual order.
    assert section_ids(["vulns", "network,ssh"]) == ["network", "ssh", "vulns"]
    assert section_ids("") == list(COLLECTORS)
    with pytest.raises(ValueError, match="Unknown section"):
        section_ids("ports,printer")
    assert clean("a\x1b[31mred\x1b[0m\nline‮evil", 40) == "a red line evil"
    assert clean("x" * 50, 10) == "x" * 9 + "…"


async def test_run_command_for_real():
    code, out, _ = await run_command([sys.executable, "-c", "print('hi')"], 10)
    assert (code, out.strip()) == (0, "hi")
    assert (await run_command(["no-such-program-bagley"], 5))[0] == 127
    code, _, err = await run_command([sys.executable, "-c", "import time; time.sleep(5)"], 0.3)
    assert code == 124 and "timed out" in err


# Briefing automation -----------------------------------------------------------------------------


def due_now(rt, item):
    rt.store.update_automation(item["id"], next_run=time.time() - 1)


async def test_briefing_automation_end_to_end(make_runtime, mock, tmp_path):
    rt = make_runtime()
    _, ctx = kali_laptop(tmp_path, store=rt.store)
    rt.services["watchdog"] = ctx
    seen = []

    async def listener(event):
        seen.append(event)

    rt.listeners.add(listener)
    item = rt.scheduler.create("briefing", "Morning briefing", "daily at 07:30")
    due_now(rt, item)
    await rt.scheduler.tick()
    done = rt.store.get_automation(item["id"])
    assert done["last_status"] == "crit" and done["enabled"]
    assert done["last_result"].startswith("4 CRIT · 3 WARN")
    assert done["state"]["counts"]["crit"] == 4
    messages = rt.store.list_messages(done["conversation_id"])
    assert [m["role"] for m in messages] == ["assistant"]  # No prompt, so no model run.
    assert messages[0]["content"].startswith("**MORNING BRIEFING // 081026 // ")
    assert messages[0]["meta"]["watchdog"]["status"] == "crit"
    note = [e for e in seen if e["type"] == "notification"][-1]
    assert note["level"] == "critical" and note["conversation_id"] == done["conversation_id"]
    assert note["title"].startswith("MORNING BRIEFING // 081026 // ")
    assert note["body"] == done["last_result"] and note["body"].startswith("4 CRIT · 3 WARN //")
    assert mock.requests == []


async def test_briefing_with_instructions_asks_the_model_without_tools(
    make_runtime, mock, tmp_path
):
    rt = make_runtime()
    _, ctx = kali_laptop(tmp_path, store=rt.store)
    rt.services["watchdog"] = ctx
    item = rt.scheduler.create(
        "briefing",
        "Evening check",
        "daily at 19:00",
        prompt="Keep it to three lines.",
        target="ssh,vulns",
    )
    mock.script = [Reply(text="Block 45.155.205.9 and update openssl.")]
    due_now(rt, item)
    await rt.scheduler.tick()
    done = rt.store.get_automation(item["id"])
    messages = rt.store.list_messages(done["conversation_id"])
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant"]
    assert messages[0]["content"].startswith("**EVENING CHECK // ")
    assert messages[-1]["content"] == "Block 45.155.205.9 and update openssl."
    run = next(r for r in mock.requests if "[Briefing" in str(r["messages"]))
    assert not run.get("tools")
    prompt = run["messages"][-1]["content"]
    assert "untrusted data" in prompt and "Keep it to three lines." in prompt
    assert "45.155.205.9" in prompt and "[CRIT] SSH" in prompt
    assert [s["id"] for s in messages[0]["meta"]["watchdog"]["sections"]] == ["ssh", "vulns"]
    assert done["last_status"] == "crit"


def test_briefing_kind_validation(make_runtime):
    rt = make_runtime()
    with pytest.raises(ScheduleError, match="15 minutes"):
        rt.scheduler.create("briefing", "Too often", "every 5 minutes")
    with pytest.raises(ScheduleError, match="Unknown section"):
        rt.scheduler.create("briefing", "Bad", "daily at 07:30", target="ports,printer")
    item = rt.scheduler.create("briefing", "", "every 30 minutes", target="ports")
    assert item["name"] == "Briefing" and item["prompt"] == ""


# Tools -------------------------------------------------------------------------------------------


async def test_tools(make_runtime, tmp_path):
    from bagley import policy
    from bagley.tools.watchdog import security_check, system_health

    rt = make_runtime()
    _, ctx = kali_laptop(tmp_path, store=rt.store)
    rt.services["watchdog"] = ctx
    tool_ctx = rt.tool_context()
    result = json.loads(await system_health.invoke({"sections": ["battery", "disks"]}, tool_ctx))
    assert [s["id"] for s in result["sections"]] == ["disks", "battery"]
    assert system_health.parameters["properties"]["sections"]["items"]["enum"] == list(COLLECTORS)
    result = json.loads(await security_check.invoke({}, tool_ctx))
    assert [s["id"] for s in result["sections"]] == ["network", "ports", "ssh", "vulns"]
    assert result["status"] == "crit"
    with pytest.raises(ToolError, match="Unknown section"):
        await system_health.invoke({"sections": "printer"}, tool_ctx)
    assert (system_health.risk, system_health.category) == ("safe", "system")
    assert (security_check.risk, security_check.category) == ("safe", "security")
    assert {"system_health", "security_check"} <= policy.READS_PRIVATE & policy.BRINGS_WEB


def test_tools_register_on_linux_or_when_asked(config, monkeypatch):
    from bagley.toolroute import select_tools
    from bagley.tools import build_registry
    from bagley.tools.watchdog import available

    platform = sys.platform
    monkeypatch.delenv("BAGLEY_WATCHDOG", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not available(config)
    monkeypatch.setattr(sys, "platform", "linux")
    assert available(config)
    monkeypatch.setenv("BAGLEY_WATCHDOG", "0")
    assert not available(config)
    monkeypatch.setenv("BAGLEY_WATCHDOG", "1")
    monkeypatch.setattr(sys, "platform", "win32")
    assert available(config)
    monkeypatch.setattr(sys, "platform", platform)
    config.ensure_dirs()
    tools = list(build_registry(config).tools.values())

    def picked(text):
        return {t.name for t in select_tools(tools, [{"role": "user", "content": text}])}

    assert "security_check" in picked("Any open ports or new devices on my network?")
    assert "security_check" in picked("check for vulnerabilities and ssh login attempts")
    assert "security_check" not in picked("What's the weather in Porto? Any support for it?")
    assert "system_health" in picked("Is my laptop healthy?")


# API ---------------------------------------------------------------------------------------------


@pytest.fixture
def client(make_runtime, tmp_path):
    rt = make_runtime()
    _, ctx = kali_laptop(tmp_path, store=rt.store)
    rt.services["watchdog"] = ctx
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        c.runtime = rt
        yield c


def test_api(client):
    sections = client.get("/api/watchdog/sections").json()
    assert [s["id"] for s in sections] == list(COLLECTORS)
    assert {s["id"] for s in sections if s["baseline"]} == {"network", "ports"}
    assert all(s["title"] and s["description"] for s in sections)

    assert client.get("/api/watchdog/last").status_code == 404
    report = client.get("/api/watchdog/report", params={"sections": "ssh,ports"}).json()
    assert [s["id"] for s in report["sections"]] == ["ports", "ssh"]
    assert report["level"] == "critical" and report["headline"].startswith("1 CRIT")
    bad = client.get("/api/watchdog/report", params={"sections": "printer"})
    assert bad.status_code == 422 and "Unknown section" in bad.json()["detail"]

    full = client.get("/api/watchdog/report").json()
    assert len(full["sections"]) == len(COLLECTORS)
    assert client.get("/api/watchdog/last").json()["headline"] == full["headline"]

    reset_ports = client.post("/api/watchdog/baseline/reset", json={"sections": ["ports"]})
    assert reset_ports.json() == {"sections": ["ports"], "removed": 1}
    everything = client.post("/api/watchdog/baseline/reset").json()
    assert everything == {"sections": ["network", "ports"], "removed": 2}
    assert client.post("/api/watchdog/baseline/reset", json={"sections": ["x"]}).status_code == 422

    created = client.post("/api/watchdog/briefing").json()
    assert created["created"] is True and created["kind"] == "briefing"
    assert (
        created["name"] == "Morning briefing" and created["schedule_text"] == "Every day at 07:30"
    )
    again = client.post("/api/watchdog/briefing").json()
    assert again["created"] is False and again["id"] == created["id"]
    listed = client.get("/api/automations").json()
    assert [a["kind"] for a in listed] == ["briefing"]


# CLI ---------------------------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, tmp_path):
    from bagley.commands import watchdog as command

    monkeypatch.setenv("BAGLEY_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    holder = {}

    def make_context(store):
        fake, ctx = kali_laptop(tmp_path / "sys-root", store=store)
        holder["fake"] = fake
        return ctx

    (tmp_path / "sys-root").mkdir()
    monkeypatch.setattr(command, "make_context", make_context)
    return holder


def test_cli_briefing(cli, capsys, monkeypatch):
    from bagley import notify
    from bagley.cli import main

    assert main(["briefing", "--json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "crit" and len(data["sections"]) == len(COLLECTORS)

    assert main(["briefing", "--sections", "battery", "services"]) == 2
    out = capsys.readouterr().out
    assert out.startswith("SYSTEM BRIEFING // ") and "\033[" not in out  # Not a terminal.
    assert "[CRIT] SERVICES  3 FAILED UNITS" in out and "[WARN] BATTERY" in out

    assert main(["briefing", "-s", "battery", "--markdown"]) == 1
    assert capsys.readouterr().out.startswith("**SYSTEM BRIEFING // ")

    sent = []

    async def fan_out(prefs, http, note, desktop=None):
        sent.append((note, desktop))
        return {}

    monkeypatch.setattr(notify, "fan_out", fan_out)
    assert main(["briefing", "-s", "network", "--notify"]) == 0  # Baseline recorded: INFO.
    note, desktop = sent[0]
    assert note.level == "info" and note.body == "ALL CLEAR // 1 OK" and desktop is True
    capsys.readouterr()

    assert main(["briefing", "-s", "printer"]) == 64
    assert "Unknown section" in capsys.readouterr().err

    assert main(["watchdog", "baseline-reset", "--sections", "network", "ports"]) == 0
    out = capsys.readouterr().out
    assert "BASELINE CLEARED // NETWORK, PORTS" in out and "3 record(s)" in out


def test_cli_colours(monkeypatch):
    from bagley.commands.watchdog import Paint

    class Tty:
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    paint = Paint(Tty())
    assert paint("ok", "[OK]") == "\033[38;2;0;250;154m[OK]\033[0m"
    assert paint("crit", "[CRIT]") == "\033[1;38;2;252;62;56m[CRIT]\033[0m"
    assert paint("warn", "[WARN]") == "\033[38;2;255;255;255m[WARN]\033[0m"
    assert paint("label", "SSH") == "\033[38;2;122;122;122mSSH\033[0m"
    monkeypatch.setenv("NO_COLOR", "1")
    assert Paint(Tty())("ok", "[OK]") == "[OK]"
