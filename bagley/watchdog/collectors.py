"""Collectors: each checks one part of the system and returns a ``Section``.

A collector is an async function taking a ``Context``. Programs run through ``ctx.run``, an
injectable runner returning ``(exit code, stdout, stderr)``, so tests replace every program
with canned output. A collector never raises for a missing program, a permission problem or
another platform: it returns an ``unavailable`` section that says why.

Everything read here can be written by someone else (journal lines, SSH user names, host
names, network names), so text is stripped of control characters and cut short before it is
shown, and the briefing tells the model it is untrusted.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

if TYPE_CHECKING:
    from bagley.watchdog.baseline import Baseline

log = logging.getLogger("bagley.watchdog")

# Runs a program: (argument list, timeout in seconds) -> (exit code, stdout, stderr).
Runner = Callable[[list[str], float], Awaitable[tuple[int, str, str]]]

NOT_FOUND, DENIED, TIMED_OUT = 127, 126, 124
MAX_OUTPUT = 8_000_000  # Bytes kept from a program's stdout.
STATUSES = ("ok", "info", "warn", "crit", "unavailable")
RANK = {"unavailable": -1, "ok": 0, "info": 1, "warn": 2, "crit": 3}


async def _read(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    buf = bytearray()
    if stream is None:
        return b""
    while chunk := await stream.read(65536):
        if len(buf) < limit:
            buf += chunk[: limit - len(buf)]
    return bytes(buf)


async def run_command(args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """Run a program without a shell, in the C locale so output parses the same everywhere."""
    exe = shutil.which(args[0])
    if not exe:
        return NOT_FOUND, "", f"{args[0]}: command not found"
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "SYSTEMD_PAGER": "",
        "SYSTEMD_COLORS": "0",
        "NO_COLOR": "1",
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args[1:],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except PermissionError as exc:
        return DENIED, "", str(exc)
    except OSError as exc:
        return NOT_FOUND, "", str(exc)
    try:
        out, err, _ = await asyncio.wait_for(
            asyncio.gather(
                _read(proc.stdout, MAX_OUTPUT), _read(proc.stderr, 200_000), proc.wait()
            ),
            timeout,
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 2)
        return TIMED_OUT, "", f"{args[0]} timed out after {timeout:g}s"
    except asyncio.CancelledError:  # The collector's own timeout: don't leave it running.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    code = proc.returncode if proc.returncode is not None else 1
    return code, out.decode(errors="replace"), err.decode(errors="replace")


@dataclass
class Context:
    """What collectors may use. Tests swap any part for a fake."""

    run: Runner = run_command
    which: Callable[[str], str | None] = shutil.which
    clock: Callable[[], float] = time.time
    baseline: Baseline | None = None
    platform: str = sys.platform
    sysfs: Path = Path("/sys")
    psutil: Any = psutil
    os_release: Path = Path("/etc/os-release")
    apt_lists: Path = Path("/var/lib/apt/lists")
    auth_log: Path = Path("/var/log/auth.log")
    # Collector runs in progress, shared by reports asked for at the same time.
    running: dict[str, asyncio.Future[Section]] = field(default_factory=dict, repr=False)

    @property
    def linux(self) -> bool:
        return self.platform.startswith("linux")

    def has(self, program: str) -> bool:
        return self.which(program) is not None

    def distro(self) -> dict[str, str]:
        """``/etc/os-release`` as a dict (ID, VERSION_CODENAME, ...); empty when unreadable."""
        try:
            text = self.os_release.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        out = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().isidentifier():
                out[key.strip()] = value.strip().strip("'\"")
        return out


@dataclass
class Finding:
    severity: str  # info | warn | crit
    text: str
    detail: str = ""


@dataclass
class Section:
    id: str
    title: str
    status: str = "ok"  # ok | info | warn | crit | unavailable
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def add(self, severity: str, text: str, detail: str = "") -> None:
        """Add a finding and raise the section's status to its severity."""
        self.findings.append(Finding(severity, clean(text, 160), clean(detail, 240)))
        self.raise_to(severity)

    def raise_to(self, status: str) -> None:
        if RANK[status] > RANK[self.status]:
            self.status = status

    def unavailable(self, reason: str) -> Section:
        self.status = "unavailable"
        self.summary = reason
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "findings": [
                {"severity": f.severity, "text": f.text, "detail": f.detail} for f in self.findings
            ],
            "data": self.data,
        }


CollectorFunc = Callable[[Context], Awaitable[Section]]


@dataclass
class Collector:
    id: str
    title: str
    description: str
    func: CollectorFunc
    timeout: float = 20.0
    baseline: bool = False  # Compares with a stored baseline that can be reset.


COLLECTORS: dict[str, Collector] = {}


def collector(
    id: str, title: str, description: str, *, timeout: float = 20.0, baseline: bool = False
) -> Callable[[CollectorFunc], CollectorFunc]:
    def wrap(func: CollectorFunc) -> CollectorFunc:
        COLLECTORS[id] = Collector(id, title, description, func, timeout, baseline)
        return func

    return wrap


# Helpers ----------------------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Control characters, zero-width and bidi overrides (they can disguise text).
_CONTROL = re.compile(
    _ANSI.pattern + r"|[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]"
)


def clean(text: Any, limit: int = 160) -> str:
    """Untrusted text for display: no control characters or ANSI codes, one line, short."""
    plain = " ".join(_CONTROL.sub(" ", str(text or "")).split())
    return plain if len(plain) <= limit else plain[: limit - 1] + "…"


def _why(code: int, err: str, program: str) -> str:
    """A short reason a program gave no usable answer."""
    text = err.lower()
    if code == NOT_FOUND:
        return f"{program.upper()} NOT INSTALLED"
    if code == DENIED or "permission denied" in text or "access denied" in text:
        return f"{program.upper()}: PERMISSION DENIED"
    if code == TIMED_OUT:
        return f"{program.upper()} TIMED OUT"
    first = clean(err.strip().splitlines()[0] if err.strip() else f"exit {code}", 100)
    return f"{program.upper()} FAILED: {first}"


def _json_lines(text: str) -> Iterator[dict[str, Any]]:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        with contextlib.suppress(ValueError):
            item = json.loads(line)
            if isinstance(item, dict):
                yield item


def _journal_message(entry: dict[str, Any]) -> str:
    msg = entry.get("MESSAGE", "")
    if isinstance(msg, list):  # Non-UTF-8 messages come as a list of byte values.
        with contextlib.suppress(TypeError, ValueError):
            msg = bytes(int(b) & 0xFF for b in msg).decode(errors="replace")
    return msg if isinstance(msg, str) else str(msg)


def _journal_limited(err: str) -> bool:
    """journalctl only showed the user's own journal (not in systemd-journal or wheel)."""
    text = err.lower()
    return "not seeing messages from other users" in text or "insufficient permissions" in text


def _journal_missing(err: str) -> bool:
    """No journal at all (a container, or systemd isn't running): nothing is known."""
    return "no journal files were found" in err.lower()


JOURNAL_PROBE = ["journalctl", "-n", "1", "-o", "json", "--no-pager"]


async def _journal_readable(ctx: Context) -> bool:
    """Whether any journal entry at all is readable. With JSON output journalctl stays silent
    when there is no journal, so an empty answer alone doesn't mean "nothing happened"."""
    _, out, _ = await ctx.run(JOURNAL_PROBE, 10)
    return any(True for _ in _json_lines(out))


JOURNAL_HINT = "Add your user to the adm group to read system logs: sudo usermod -aG adm $USER"


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def _is_external(text: str) -> bool:
    addr = _ip(text)
    if addr is None:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global


# 1. Services --------------------------------------------------------------------------------------

# A failure of these weakens the machine's defences, so it is critical rather than a warning.
CRITICAL_UNITS = {"nftables", "iptables", "ip6tables", "ufw", "firewalld", "fail2ban", "apparmor",
                  "auditd", "sshguard", "systemd-journald", "polkit"}  # fmt: skip


async def _failed_units(ctx: Context, scope: list[str]) -> tuple[list[dict[str, str]] | None, str]:
    base = ["systemctl", *scope, "--failed", "--no-legend", "--plain", "--no-pager"]
    code, out, err = await ctx.run([*base, "--output=json"], 15)
    if code == 0:
        with contextlib.suppress(ValueError):
            rows = json.loads(out or "[]")
            if isinstance(rows, list):
                return [
                    {
                        "unit": clean(r.get("unit"), 80),
                        "sub": clean(r.get("sub"), 20),
                        "description": clean(r.get("description"), 100),
                    }
                    for r in rows
                    if isinstance(r, dict) and r.get("unit")
                ], ""
    code, out, err = await ctx.run(base, 15)  # Older systemd: no JSON output.
    if code != 0:
        return None, _why(code, err, "systemctl")
    units = []
    for line in out.splitlines():
        parts = line.strip().lstrip("●* ").split(None, 4)
        if len(parts) >= 4 and "." in parts[0]:
            units.append(
                {
                    "unit": clean(parts[0], 80),
                    "sub": clean(parts[3], 20),
                    "description": clean(parts[4] if len(parts) > 4 else "", 100),
                }
            )
    return units, ""


@collector("services", "SERVICES", "Failed systemd units, system and user")
async def services(ctx: Context) -> Section:
    sec = Section("services", "SERVICES")
    if not ctx.linux or not ctx.has("systemctl"):
        return sec.unavailable("NO SYSTEMD ON THIS SYSTEM")
    (system, why), (user, _) = await asyncio.gather(
        _failed_units(ctx, []), _failed_units(ctx, ["--user"])
    )
    if system is None and user is None:
        return sec.unavailable(why)
    failed = [(u, "system") for u in system or []] + [(u, "user") for u in user or []]
    for unit, scope in failed:
        name = unit["unit"]
        stem = name.rsplit(".", 1)[0].split("@", 1)[0]
        label = name if scope == "system" else f"{name} (user)"
        severity = "crit" if scope == "system" and stem in CRITICAL_UNITS else "warn"
        sec.add(severity, f"{label} {unit['sub'].upper() or 'FAILED'}", unit["description"])
    sec.data = {
        "system": system,
        "user": user,  # None when there is no user session bus.
    }
    if not failed:
        sec.summary = "NO FAILED UNITS"
    else:
        names = ", ".join(u["unit"] for u, _ in failed[:3]) + (" …" if len(failed) > 3 else "")
        sec.summary = f"{len(failed)} FAILED UNIT{'S' if len(failed) > 1 else ''} // {names}"
    return sec


# 2. Journal -------------------------------------------------------------------------------------


JOURNAL_LIMIT = 500
JOURNAL_ERRORS = ["journalctl", "-p", "err", "-b", "--since", "24 hours ago", "-o", "json",
                  "--no-pager", "-n", str(JOURNAL_LIMIT)]  # fmt: skip


@collector("journal", "JOURNAL", "Errors logged in the last 24 hours, grouped by unit")
async def journal(ctx: Context) -> Section:
    sec = Section("journal", "JOURNAL")
    if not ctx.linux or not ctx.has("journalctl"):
        return sec.unavailable("NO SYSTEMD JOURNAL ON THIS SYSTEM")
    code, out, err = await ctx.run(JOURNAL_ERRORS, 20)
    entries = list(_json_lines(out))
    if _journal_missing(err) and not entries:
        return sec.unavailable("NO JOURNAL FILES FOUND")
    if code != 0 and not entries:
        return sec.unavailable(_why(code, err, "journalctl"))
    if not entries and not await _journal_readable(ctx):
        return sec.unavailable("NO READABLE JOURNAL ENTRIES")
    groups: Counter[str] = Counter()
    last: dict[str, str] = {}
    worst: dict[str, int] = {}
    for entry in entries:
        key = clean(
            entry.get("_SYSTEMD_UNIT")
            or entry.get("_SYSTEMD_USER_UNIT")
            or entry.get("SYSLOG_IDENTIFIER")
            or entry.get("_COMM")
            or ("kernel" if entry.get("_TRANSPORT") == "kernel" else "unknown"),
            60,
        )
        groups[key] += 1
        last[key] = _journal_message(entry)
        try:
            priority = int(entry.get("PRIORITY", 3))
        except (TypeError, ValueError):
            priority = 3
        worst[key] = min(worst.get(key, 7), priority)
    total = len(entries)
    top = groups.most_common(5)
    for key, count in top:
        severity = "warn" if worst[key] <= 2 else "info"  # crit, alert or emerg.
        sec.add(severity, f"{key} ×{count}", last[key])
    if total >= 200:
        sec.raise_to("warn")
    shown = f"{total}+" if total >= JOURNAL_LIMIT else str(total)
    sec.summary = (
        "NO ERRORS // 24H"
        if not total
        else f"{shown} JOURNAL ERRORS // {len(groups)} SOURCE{'S' if len(groups) > 1 else ''}"
    )
    sec.data = {
        "total": total,
        "truncated": total >= JOURNAL_LIMIT,
        "groups": [{"source": k, "count": c, "last": clean(last[k], 200)} for k, c in top],
    }
    if _journal_limited(err):
        sec.data["limited"] = True
        sec.add("info", "ONLY YOUR OWN JOURNAL IS READABLE", JOURNAL_HINT)
    return sec


# 3. Disks ---------------------------------------------------------------------------------------

SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2",
           "autofs", "devpts", "efivarfs", "nsfs", "tracefs", "ramfs", "iso9660", "debugfs",
           "securityfs", "pstore", "bpf", "configfs", "mqueue", "hugetlbfs", "fusectl",
           "binfmt_misc", "rpc_pipefs", "nfs", "nfs4", "cifs", "smb3", "smbfs", "9p",
           "fuse.sshfs", "fuse.rclone", "fuse.gvfsd-fuse", "fuse.portal", "fuse.snapfuse",
           "fuse.lxcfs"}  # fmt: skip
SKIP_MOUNTS = ("/snap/", "/var/lib/docker/", "/var/lib/containers/", "/run/", "/proc", "/sys",
               "/dev")  # fmt: skip
DISK_WARN, DISK_CRIT = 85, 95
DEVICE = re.compile(r"^/dev/[\w./-]+$")


def _filesystems(ctx: Context) -> list[dict[str, Any]]:
    """Real filesystems, one per device (btrfs subvolumes share their device's usage)."""
    by_device: dict[str, dict[str, Any]] = {}
    for part in ctx.psutil.disk_partitions(all=False):
        fstype = (part.fstype or "").lower()
        mount = part.mountpoint
        if fstype in SKIP_FS or mount.startswith(SKIP_MOUNTS) or mount in ("/run", "/dev"):
            continue
        try:
            usage = ctx.psutil.disk_usage(mount)
        except (OSError, PermissionError):
            continue
        if not usage.total:
            continue
        device = part.device or mount
        known = by_device.get(device)
        if known and len(known["mount"]) <= len(mount):
            known["also"].append(mount)
            continue
        by_device[device] = {
            "mount": mount,
            "device": device,
            "fstype": fstype,
            "percent": round(float(usage.percent)),
            "free_gb": round(usage.free / 1024**3, 1),
            "total_gb": round(usage.total / 1024**3, 1),
            "also": [known["mount"], *known["also"]] if known else [],
        }
    return sorted(by_device.values(), key=lambda f: f["mount"])


def _base_disk(device: str) -> str | None:
    """/dev/nvme0n1p2 -> /dev/nvme0n1, /dev/sda1 -> /dev/sda; None for mappers and loops."""
    if m := re.fullmatch(r"(/dev/nvme\d+n\d+)(?:p\d+)?", device):
        return m.group(1)
    if m := re.fullmatch(r"(/dev/(?:sd|vd|hd|xvd)[a-z]+)\d*", device):
        return m.group(1)
    if m := re.fullmatch(r"(/dev/mmcblk\d+)(?:p\d+)?", device):
        return m.group(1)
    return None


async def _smart(ctx: Context, filesystems: list[dict[str, Any]]) -> dict[str, str]:
    """SMART health per disk, where smartctl works without root. Quiet when it doesn't."""
    devices: list[str] = []
    _, out, _ = await ctx.run(["smartctl", "--scan", "-j"], 10)
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        devices = [d["name"] for d in json.loads(out).get("devices", []) if d.get("name")]
    if not devices:
        devices = sorted({d for f in filesystems if (d := _base_disk(f["device"]))})
    devices = [d for d in devices if DEVICE.match(d)][:8]

    async def health(device: str) -> tuple[str, str]:
        _, out, _ = await ctx.run(["smartctl", "-H", "-j", device], 15)
        try:
            data = json.loads(out)
        except ValueError:
            data = {}
        status = data.get("smart_status") if isinstance(data, dict) else None
        if isinstance(status, dict) and "passed" in status:
            return device, "PASSED" if status["passed"] else "FAILED"
        return device, "NO ACCESS"  # Usually needs root; skipped without a finding.

    return dict(await asyncio.gather(*(health(d) for d in devices)))


@collector("disks", "DISKS", "Space on each filesystem and SMART health of the disks")
async def disks(ctx: Context) -> Section:
    sec = Section("disks", "DISKS")
    try:
        filesystems = await asyncio.to_thread(_filesystems, ctx)
    except Exception as exc:
        return sec.unavailable(f"DISK USAGE UNREADABLE: {clean(exc, 80)}")
    if not filesystems:
        return sec.unavailable("NO FILESYSTEMS FOUND")
    full = [f for f in filesystems if f["percent"] >= DISK_WARN]
    for fs in sorted(full, key=lambda f: -f["percent"]):
        sec.add(
            "crit" if fs["percent"] >= DISK_CRIT else "warn",
            f"{fs['mount']} {fs['percent']}%",
            f"{fs['free_gb']} GB free of {fs['total_gb']} GB ({fs['fstype']})",
        )
    smart: dict[str, str] = {}
    if ctx.linux and ctx.has("smartctl"):
        smart = await _smart(ctx, filesystems)
        for device, state in smart.items():
            if state == "FAILED":
                sec.add("crit", f"SMART FAILING {device}", "Back up this disk now.")
    sec.data = {"filesystems": filesystems, "smart": smart}
    if full:
        sec.summary = " · ".join(f.text for f in sec.findings[: min(len(full), 3)])
    else:
        fullest = max(filesystems, key=lambda f: f["percent"])
        sec.summary = (
            f"{len(filesystems)} FILESYSTEM{'S' if len(filesystems) > 1 else ''} "
            f"// FULLEST {fullest['mount']} {fullest['percent']}%"
        )
    if any(s == "FAILED" for s in smart.values()):
        sec.summary = "SMART FAILURE // " + sec.summary
    return sec


# 4. Battery -------------------------------------------------------------------------------------

BATTERY_WARN, BATTERY_CRIT = 80, 60


def _read_battery(path: Path) -> dict[str, Any]:
    def text(name: str) -> str | None:
        try:
            return (path / name).read_text(errors="replace").strip()
        except OSError:
            return None

    def number(name: str) -> int | None:
        value = text(name)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    full = number("energy_full") or number("charge_full")
    design = number("energy_full_design") or number("charge_full_design")
    unit = "Wh" if number("energy_full") else "Ah"
    info: dict[str, Any] = {
        "name": path.name,
        "status": clean(text("status") or "Unknown", 20),
        "capacity": number("capacity"),
        "cycles": number("cycle_count"),
        "model": clean(text("model_name") or "", 40),
    }
    if full and design:
        info["health"] = round(full / design * 100)
        info["full"] = f"{full / 1e6:.1f} {unit}"
        info["design"] = f"{design / 1e6:.1f} {unit}"
    return info


@collector("battery", "BATTERY", "Battery health (capacity left versus new), cycles and charge")
async def battery(ctx: Context) -> Section:
    sec = Section("battery", "BATTERY")
    if not ctx.linux:
        return sec.unavailable("BATTERY HEALTH NEEDS LINUX")
    base = ctx.sysfs / "class" / "power_supply"
    paths = sorted(base.glob("BAT*")) if base.is_dir() else []
    if not paths:
        return sec.unavailable("NO BATTERY FOUND")
    batteries = [await asyncio.to_thread(_read_battery, p) for p in paths]
    parts = []
    for bat in batteries:
        health = bat.get("health")
        if health is not None and health < BATTERY_WARN:
            sec.add(
                "crit" if health < BATTERY_CRIT else "warn",
                f"{bat['name']} HEALTH {health}%",
                f"{bat['full']} of {bat['design']} when new"
                + (f", {bat['cycles']} cycles" if bat.get("cycles") else ""),
            )
        bits = [bat["name"] + (f" HEALTH {health}%" if health is not None else "")]
        if bat.get("cycles"):
            bits.append(f"{bat['cycles']} CYCLES")
        if bat.get("capacity") is not None:
            bits.append(f"{bat['capacity']}% {bat['status'].upper()}")
        parts.append(" // ".join(bits))
    sec.summary = " · ".join(parts)
    sec.data = {"batteries": batteries}
    return sec


# 5. Updates -------------------------------------------------------------------------------------

# Packages whose updates usually fix security problems, by Debian and Kali names. Kali has no
# separate -security suite, so names are the signal there.
SECURITY_PACKAGES = {"openssl", "libssl3", "libssl3t64", "openssh-server", "openssh-client",
                     "openssh-sftp-server", "libc6", "libc-bin", "sudo", "systemd", "libsystemd0",
                     "udev", "firefox-esr", "firefox", "chromium", "thunderbird", "gnupg", "gpg",
                     "libgnutls30", "libgnutls30t64", "curl", "libcurl4", "libcurl4t64",
                     "polkitd", "libpolkit-gobject-1-0", "xz-utils", "liblzma5", "libnss3",
                     "zlib1g", "wpasupplicant", "network-manager", "tailscale", "bash",
                     "ca-certificates", "libpam0g", "libpam-modules", "passwd", "login",
                     "util-linux", "openvpn", "wireguard-tools", "bind9", "bind9-libs", "dnsmasq",
                     "dnsmasq-base", "libexpat1", "libxml2", "libkrb5-3", "samba", "cups",
                     "intel-microcode", "amd64-microcode", "grub-efi-amd64", "grub-pc", "git",
                     "python3", "perl", "apache2", "nginx", "exim4", "postfix"}  # fmt: skip
SECURITY_PREFIXES = ("linux-image", "linux-headers", "libssl", "openssh", "libpam", "firmware-")
APT_LINE = re.compile(r"^([^/\s]+)/(\S+)\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]")
STALE_LISTS = 2 * 86400  # After two days `apt update` is due.


def is_security_package(name: str) -> bool:
    base = name.split(":", 1)[0].lower()
    return base in SECURITY_PACKAGES or base.startswith(SECURITY_PREFIXES)


def _apt_updates(out: str) -> list[dict[str, Any]]:
    """``apt list --upgradable``: name, suites, new version, arch, old version."""
    found = []
    for line in out.splitlines():
        if m := APT_LINE.match(line.strip()):
            found.append(
                {
                    "name": m.group(1),
                    "old": m.group(4),
                    "new": m.group(3),
                    "suite": m.group(2),
                    "security": "-security" in m.group(2),  # Debian and Ubuntu, not Kali.
                }
            )
    return found


def lists_age(ctx: Context) -> float | None:
    """Seconds since the package lists were last refreshed (``apt update``), if known."""
    try:
        stamps = [
            entry.stat().st_mtime
            for entry in ctx.apt_lists.iterdir()
            if entry.name.endswith(("Release", "InRelease"))
        ]
    except OSError:
        return None
    return max(0.0, ctx.clock() - max(stamps)) if stamps else None


@collector("updates", "UPDATES", "Pending package updates (apt)", timeout=90)
async def updates(ctx: Context) -> Section:
    sec = Section("updates", "UPDATES")
    if not ctx.linux:
        return sec.unavailable("PACKAGE UPDATES NEED LINUX")
    if not ctx.has("apt"):
        return sec.unavailable("NEEDS APT (KALI, DEBIAN, UBUNTU)")
    code, out, err = await ctx.run(["apt", "list", "--upgradable"], 60)
    if code != 0:
        return sec.unavailable(_why(code, err, "apt"))
    pending = _apt_updates(out)
    for item in pending:
        item["security"] = item["security"] or is_security_package(item["name"])
    security = [p for p in pending if p["security"]]
    for item in security[:15]:
        sec.add("warn", f"{item['name']} {item['old']} -> {item['new']}")
    others = [p["name"] for p in pending if not p["security"]]
    if others:
        sec.add(
            "info",
            f"{len(others)} OTHER UPDATE{'S' if len(others) > 1 else ''}",
            ", ".join(others[:12]) + (" …" if len(others) > 12 else ""),
        )
    age = lists_age(ctx)
    if age is not None and age > STALE_LISTS:
        days = int(age // 86400)
        sec.add(
            "warn" if days >= 7 else "info",
            f"PACKAGE LISTS {days} DAYS OLD",
            "Run sudo apt update: this list is only as current as the last one.",
        )
    if not pending:
        sec.summary = "UP TO DATE"
    else:
        parts = [f"{len(pending)} UPDATE{'S' if len(pending) > 1 else ''} PENDING"]
        if security:
            parts.insert(0, f"{len(security)} SECURITY UPDATE{'S' if len(security) > 1 else ''}")
        sec.summary = " // ".join(parts)
    if age is not None and age > STALE_LISTS:
        sec.summary += f" // LISTS {int(age // 86400)}D OLD"
    sec.data.update(
        {
            "count": len(pending),
            "security": len(security),
            "lists_age": round(age) if age is not None else None,
            "packages": [
                {k: clean(p[k], 60) if isinstance(p[k], str) else p[k] for k in p}
                for p in sorted(pending, key=lambda p: (not p["security"], p["name"]))[:60]
            ],
        }
    )
    return sec


# 6. Network -------------------------------------------------------------------------------------

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
NEIGH_IGNORE = {"FAILED", "INCOMPLETE", "NOARP", "NONE"}
MAX_NETWORKS = 20


def _neighbors(out: str) -> dict[str, dict[str, Any]]:
    """Devices from ``ip -j neigh``, one per MAC address, IPv4 address preferred."""
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return {}
    devices: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        mac = str(row.get("lladdr") or "").lower()
        states = row.get("state") or []
        states = [states] if isinstance(states, str) else [str(s) for s in states]
        addr = _ip(str(row.get("dst") or ""))
        if not MAC.match(mac) or mac == "00:00:00:00:00:00" or int(mac[:2], 16) & 1:
            continue  # Missing, empty or multicast.
        if addr is None or set(states) & NEIGH_IGNORE or not states:
            continue
        ip = str(addr)
        known = devices.get(mac)
        if known is None:
            devices[mac] = {"ip": ip, "state": states[0], "dev": clean(row.get("dev"), 20)}
        elif addr.version == 4 and ":" in known["ip"]:
            known["ip"] = ip
    return devices


async def _network_name(ctx: Context, dev: str) -> str:
    """The active NetworkManager connection on ``dev`` (often the Wi-Fi name)."""
    if not dev or not ctx.has("nmcli"):
        return ""
    code, out, _ = await ctx.run(
        ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"], 10
    )
    if code != 0:
        return ""
    for line in out.splitlines():
        name, _, device = line.rpartition(":")
        if device == dev:
            return clean(name.replace("\\:", ":"), 32)
    return ""


async def _lan(ctx: Context, sec: Section, now: float) -> tuple[int, int, bool] | None:
    code, out, err = await ctx.run(["ip", "-j", "neigh"], 10)
    if code != 0:
        sec.data["lan_error"] = _why(code, err, "ip")
        return None
    devices = _neighbors(out)
    gateway, dev = "", ""
    code, out, _ = await ctx.run(["ip", "-j", "route", "show", "default"], 10)
    with contextlib.suppress(ValueError, TypeError, IndexError, AttributeError):
        route = json.loads(out)[0]
        gateway, dev = str(route.get("gateway") or ""), str(route.get("dev") or "")
    gateway_mac = next((m for m, d in devices.items() if d["ip"] == gateway), "")
    network_id = gateway_mac or (f"gw-{gateway}" if _ip(gateway) else "default")
    name = await _network_name(ctx, dev)
    recorded, new = False, []
    if ctx.baseline is not None:
        recorded, new = ctx.baseline.track(f"network.devices.{network_id}", devices, now)
        ctx.baseline.prune("network.devices.", MAX_NETWORKS)
    for mac in new:
        d = devices[mac]
        sec.add("warn", f"NEW DEVICE {d['ip']} {mac} ({d['state']})", f"on {d['dev']}")
    sec.data.update(
        {
            "network": {"id": network_id, "name": name, "gateway": gateway, "dev": dev},
            "devices": [
                {"ip": d["ip"], "mac": m, "state": d["state"], "new": m in new}
                for m, d in sorted(devices.items(), key=lambda kv: kv[1]["ip"])
            ][:60],
        }
    )
    return len(devices), len(new), recorded


async def _tailnet(ctx: Context, sec: Section, now: float) -> tuple[int, int, bool] | None:
    code, out, err = await ctx.run(["tailscale", "status", "--json"], 10)
    try:
        status = json.loads(out) if out.strip() else None
    except ValueError:
        status = None
    if not isinstance(status, dict):
        sec.data["tailscale"] = {"state": _why(code, err, "tailscale")}
        return None
    state = str(status.get("BackendState") or "")
    if state != "Running":
        sec.data["tailscale"] = {"state": clean(state or "STOPPED", 20).upper()}
        return None
    peers: dict[str, dict[str, Any]] = {}
    for key, peer in (status.get("Peer") or {}).items():
        if not isinstance(peer, dict):
            continue
        ips = [str(ip) for ip in peer.get("TailscaleIPs") or [] if _ip(str(ip))]
        peer_id = clean(peer.get("ID") or peer.get("PublicKey") or key, 80)
        peers[peer_id] = {
            "host": clean(peer.get("HostName") or peer.get("DNSName") or "?", 63),
            "ip": ips[0] if ips else "",
            "os": clean(peer.get("OS"), 20),
            "online": bool(peer.get("Online")),
        }
    recorded, new = False, []
    if ctx.baseline is not None:
        recorded, new = ctx.baseline.track("network.tailscale", peers, now)
    for peer_id in new:
        p = peers[peer_id]
        sec.add("warn", f"NEW TAILNET PEER {p['host']} {p['ip']} ({p['os'] or '?'})")
    sec.data["tailscale"] = {
        "state": "RUNNING",
        "peers": [
            {**p, "new": k in new} for k, p in sorted(peers.items(), key=lambda kv: kv[1]["host"])
        ][:40],
    }
    return len(peers), len(new), recorded


@collector(
    "network",
    "NETWORK",
    "Devices on the local network and Tailscale peers; new ones are flagged",
    baseline=True,
)
async def network(ctx: Context) -> Section:
    sec = Section("network", "NETWORK")
    has_ip, has_ts = ctx.linux and ctx.has("ip"), ctx.has("tailscale")
    if not has_ip and not has_ts:
        return sec.unavailable("IP AND TAILSCALE NOT FOUND")
    now = ctx.clock()
    lan = await _lan(ctx, sec, now) if has_ip else None
    tail = await _tailnet(ctx, sec, now) if has_ts else None
    if lan is None and tail is None:
        return sec.unavailable(sec.data.get("lan_error") or "NO NETWORK DATA")
    news, parts, recorded = [], [], False
    if lan is not None:
        name = sec.data["network"]["name"]
        parts.append(
            f"{lan[0]} DEVICE{'S' if lan[0] != 1 else ''}" + (f" ON {name}" if name else "")
        )
        if lan[1]:
            news.append(f"{lan[1]} NEW DEVICE{'S' if lan[1] > 1 else ''}")
        recorded |= lan[2]
    if tail is not None:
        parts.append(f"{tail[0]} TAILNET PEER{'S' if tail[0] != 1 else ''}")
        if tail[1]:
            news.append(f"{tail[1]} NEW TAILNET PEER{'S' if tail[1] > 1 else ''}")
        recorded |= tail[2]
    if recorded:
        parts.insert(0, "BASELINE RECORDED")
        sec.raise_to("info")
    sec.summary = " // ".join([" · ".join(news), *parts] if news else parts)
    return sec


# 7. Ports ---------------------------------------------------------------------------------------

SENSITIVE_PORTS = {21: "ftp", 22: "ssh", 23: "telnet", 445: "smb", 3306: "mysql", 3389: "rdp",
                   5432: "postgres", 5900: "vnc", 6379: "redis", 27017: "mongodb"}  # fmt: skip
EPHEMERAL = 32768  # Linux ephemeral ports start here; client sockets there come and go.
PROCESS = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')


def _split_addr(text: str) -> tuple[str, int] | None:
    host, _, port = text.rpartition(":")
    if not port.isdigit():
        return None
    host = host.strip("[]").split("%", 1)[0]
    return (host or "*"), int(port)


def _parse_ss(out: str) -> list[dict[str, Any]]:
    sockets = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        proto, state = cols[0].lower(), cols[1].upper()
        if (proto, state) not in (("tcp", "LISTEN"), ("udp", "UNCONN")):
            continue
        local = _split_addr(cols[4])
        if not local:
            continue
        m = PROCESS.search(line)
        sockets.append(
            {"proto": proto, "addr": local[0], "port": local[1], "process": m.group(1) if m else ""}
        )
    return sockets


def _psutil_sockets(ctx: Context) -> list[dict[str, Any]]:
    sockets = []
    for conn in ctx.psutil.net_connections(kind="inet"):
        is_tcp = conn.type == 1  # socket.SOCK_STREAM
        if is_tcp and conn.status != "LISTEN":
            continue
        if not is_tcp and (conn.raddr or not conn.laddr):
            continue
        name = ""
        if conn.pid:
            with contextlib.suppress(Exception):
                name = ctx.psutil.Process(conn.pid).name()
        sockets.append(
            {
                "proto": "tcp" if is_tcp else "udp",
                "addr": conn.laddr.ip or "*",
                "port": int(conn.laddr.port),
                "process": name,
            }
        )
    return sockets


def _scope(addr: str) -> str:
    if addr in ("*", "0.0.0.0", "::"):
        return "all"
    ip = _ip(addr)
    if ip is not None and (ip.is_loopback or (getattr(ip, "ipv4_mapped", None) or ip).is_loopback):
        return "loopback"
    return "interface"


def _socket_key(s: dict[str, Any]) -> str:
    addr = f"[{s['addr']}]" if ":" in s["addr"] else s["addr"]
    return f"{s['proto']} {addr}:{s['port']}"


@collector(
    "ports",
    "PORTS",
    "Listening ports; new ones are flagged, exposed sensitive ports are critical",
    baseline=True,
)
async def ports(ctx: Context) -> Section:
    sec = Section("ports", "PORTS")
    sockets: list[dict[str, Any]] | None = None
    if ctx.linux and ctx.has("ss"):
        code, out, _ = await ctx.run(["ss", "-H", "-tulpn"], 10)
        if code == 0:
            sockets = _parse_ss(out)
    if sockets is None:
        try:
            sockets = await asyncio.to_thread(_psutil_sockets, ctx)
        except Exception as exc:  # psutil.AccessDenied on macOS without root, and others.
            return sec.unavailable(f"SOCKETS UNREADABLE: {clean(exc, 60) or 'ACCESS DENIED'}")
    items: dict[str, dict[str, Any]] = {}
    for s in sockets:
        scope = _scope(s["addr"])
        if s["port"] >= EPHEMERAL and (s["proto"] == "udp" or scope == "loopback"):
            continue
        key = _socket_key(s)
        if key in items and items[key]["process"]:
            continue
        items[key] = {"process": clean(s["process"], 40), "scope": scope, "port": s["port"]}
    recorded, new = False, []
    if ctx.baseline is not None:
        recorded, new = ctx.baseline.track("ports", items, ctx.clock())
    new.sort(key=lambda k: (-RANK[_new_port_severity(items[k])], items[k]["port"]))
    for key in new:
        item = items[key]
        sec.add(
            _new_port_severity(item),
            f"NEW {key.upper()} {item['process'] or '?'}",
            _port_hint(item),
        )
    exposed = [k for k, v in items.items() if v["scope"] == "all" and v["port"] in SENSITIVE_PORTS]
    for key in sorted(k for k in exposed if k not in new):
        item = items[key]
        sec.add("info", f"EXPOSED {key.upper()} {item['process'] or '?'}", _port_hint(item))
    parts = [f"{len(items)} LISTENING"]
    if exposed:
        parts.append(f"{len(exposed)} SENSITIVE EXPOSED")
    if new:
        worst = f"{new[0].split(' ', 1)[1]} {items[new[0]]['process']}".rstrip()
        more = f" +{len(new) - 1}" if len(new) > 1 else ""
        parts.insert(0, f"{len(new)} NEW PORT{'S' if len(new) > 1 else ''} {worst}{more}")
    if recorded:
        parts.insert(0, "BASELINE RECORDED")
        sec.raise_to("info")
    sec.summary = " // ".join(parts)
    sec.data = {
        "sockets": [
            {"key": k, **v, "new": k in new}
            for k, v in sorted(items.items(), key=lambda kv: kv[1]["port"])
        ][:80],
        "process_names": any(v["process"] for v in items.values()),
    }
    return sec


def _new_port_severity(item: dict[str, Any]) -> str:
    if item["scope"] == "loopback":
        return "info"  # Reachable from this computer only.
    if item["scope"] == "all" and item["port"] in SENSITIVE_PORTS:
        return "crit"
    return "warn"


def _port_hint(item: dict[str, Any]) -> str:
    where = {"all": "on every interface", "loopback": "on this computer only"}.get(
        item["scope"], "on one interface"
    )
    service = SENSITIVE_PORTS.get(item["port"])
    return f"{service} {where}" if service else where


# 8. SSH -----------------------------------------------------------------------------------------

FAILED = re.compile(
    r"Failed (?:password|keyboard-interactive/pam) for (?:invalid user )?(\S*) from (\S+) port (\d+)"
)
INVALID = re.compile(r"Invalid user (\S*) from (\S+)(?: port (\d+))?")
MAX_AUTH = re.compile(
    r"maximum authentication attempts exceeded for (?:invalid user )?(\S*) from (\S+) port (\d+)"
)
PAM_FAILURE = re.compile(r"authentication failure;.*?\brhost=(\S+)(?:.*?\buser=(\S+))?")
ACCEPTED = re.compile(r"Accepted (\S+) for (\S+) from (\S+) port (\d+)")
SSH_CRIT = 20
SSH_JOURNAL = ["journalctl", "-u", "sshd", "-u", "ssh", "-u", "sshd@*", "--since", "24 hours ago",
               "-o", "json", "--no-pager", "-n", "5000"]  # fmt: skip


def _ssh_events(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Failed attempts per source address, and accepted logins.

    One attempt shows up in several lines (Invalid user, Failed password, a PAM line), so
    attempts are counted per connection (address and port), and PAM lines only for addresses
    sshd itself didn't report."""
    conns: dict[tuple[str, str], dict[str, Any]] = {}
    pam: dict[str, dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    for n, entry in enumerate(entries):
        msg = _journal_message(entry)
        try:
            ts = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        except (TypeError, ValueError):
            ts = 0.0
        if m := FAILED.search(msg):
            c = conns.setdefault(
                (m.group(2), m.group(3)), {"user": m.group(1), "failed": 0, "ts": ts}
            )
            c["failed"] += 1
        elif m := MAX_AUTH.search(msg):
            conns.setdefault((m.group(2), m.group(3)), {"user": m.group(1), "failed": 0, "ts": ts})
        elif m := INVALID.search(msg):
            conns.setdefault(
                (m.group(2), m.group(3) or f"#{n}"), {"user": m.group(1), "failed": 0, "ts": ts}
            )
        elif m := PAM_FAILURE.search(msg):
            p = pam.setdefault(m.group(1), {"count": 0, "users": Counter(), "first": ts})
            p["count"] += 1
            if m.group(2):
                p["users"][m.group(2)] += 1
        elif m := ACCEPTED.search(msg):
            accepted.append({"method": m.group(1), "user": m.group(2), "ip": m.group(3), "ts": ts})
    sources: dict[str, dict[str, Any]] = {}
    for (ip, _), c in conns.items():
        s = sources.setdefault(ip, {"count": 0, "users": Counter(), "first": c["ts"]})
        s["count"] += max(c["failed"], 1)
        s["users"][c["user"] or "?"] += 1
        s["first"] = min(s["first"], c["ts"])
    for ip, p in pam.items():
        if ip not in sources:
            sources[ip] = p
    return {ip: s for ip, s in sources.items() if _ip(ip)}, accepted


AUTH_LOG_TAIL = 8_000_000  # Bytes read from the end of /var/log/auth.log.
ISO_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+\S+\s+"
    r"([\w.-]+?)(?:\[\d+\])?:\s(.*)$"
)
SYSLOG_LINE = re.compile(
    r"^([A-Z][a-z]{2})\s+(\d{1,2})\s(\d{2}):(\d{2}):(\d{2})\s+\S+\s+([\w.-]+?)(?:\[\d+\])?:\s(.*)$"
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _syslog_time(m: re.Match[str], now: float) -> float | None:
    """Classic syslog time has no year: this year, or last year if that would be the future."""
    if m.group(1) not in MONTHS:
        return None
    month, day = MONTHS.index(m.group(1)) + 1, int(m.group(2))
    hour, minute, second = (int(m.group(i)) for i in (3, 4, 5))
    year = time.localtime(now).tm_year
    for y in (year, year - 1):
        try:
            ts = time.mktime((y, month, day, hour, minute, second, 0, 0, -1))
        except (OverflowError, ValueError):
            return None
        if ts <= now + 86400:
            return ts
    return None


def auth_log_entries(text: str, now: float, since: float = 86400) -> list[dict[str, Any]]:
    """sshd lines of the last day from an auth.log (rsyslog's ISO or the classic format), as
    journal-like entries."""
    from datetime import datetime

    entries = []
    for line in text.splitlines():
        if "sshd" not in line:
            continue
        ts: float | None = None
        if m := ISO_LINE.match(line):
            stamp = m.group(1).replace("Z", "+00:00")
            if re.search(r"[+-]\d{4}$", stamp):
                stamp = stamp[:-2] + ":" + stamp[-2:]
            try:
                ts = datetime.fromisoformat(
                    stamp[:26] + stamp[26:].lstrip("0123456789")
                ).timestamp()
            except ValueError:
                ts = None
            program, message = m.group(2), m.group(3)
        elif m := SYSLOG_LINE.match(line):
            ts = _syslog_time(m, now)
            program, message = m.group(6), m.group(7)
        else:
            continue
        if ts is None or not program.startswith("sshd") or ts < now - since:
            continue  # OpenSSH 9.8+ logs as sshd-session.
        entries.append({"MESSAGE": message, "__REALTIME_TIMESTAMP": str(int(ts * 1e6))})
    return entries


def _read_tail(path: Path, limit: int) -> str | None:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


async def _ssh_journal(ctx: Context) -> tuple[list[dict[str, Any]] | None, str]:
    """sshd's journal entries, or None and the reason they can't be read."""
    if not ctx.has("journalctl"):
        return None, "NO SYSTEMD JOURNAL ON THIS SYSTEM"
    code, out, err = await ctx.run(SSH_JOURNAL, 20)
    entries = list(_json_lines(out))
    if _journal_limited(err):
        return None, "NO ACCESS TO THE SYSTEM JOURNAL // JOIN adm"
    if _journal_missing(err) and not entries:
        return None, "NO JOURNAL FILES FOUND"
    if code != 0 and not entries:
        return None, _why(code, err, "journalctl")
    if not entries and not await _journal_readable(ctx):
        return None, "NO READABLE JOURNAL ENTRIES"
    return entries, ""


@collector("ssh", "SSH", "Failed SSH logins in the last 24 hours, by source address and user")
async def ssh(ctx: Context) -> Section:
    sec = Section("ssh", "SSH")
    if not ctx.linux:
        return sec.unavailable("SSH LOGINS NEED LINUX")
    entries, reason = await _ssh_journal(ctx)
    source = "journal"
    if entries is None:
        # Kali and Debian with rsyslog also keep /var/log/auth.log (readable by the adm group).
        text = _read_tail(ctx.auth_log, AUTH_LOG_TAIL)
        if text is None:
            return sec.unavailable(reason)
        entries, source = auth_log_entries(text, ctx.clock()), "auth.log"
    active = None
    if ctx.has("systemctl"):
        _, out, _ = await ctx.run(
            ["systemctl", "is-active", "sshd", "ssh", "sshd.socket", "ssh.socket"], 10
        )
        states = [line.strip() for line in out.splitlines() if line.strip()]
        active = "active" in states if states else None  # None: systemd didn't answer.
    sources, accepted = _ssh_events(entries)
    total = sum(s["count"] for s in sources.values())
    external = sum(s["count"] for ip, s in sources.items() if _is_external(ip))
    ranked = sorted(sources.items(), key=lambda kv: kv[1]["count"], reverse=True)
    for ip, s in ranked[:8]:
        users = ", ".join(clean(u, 24) for u, _ in s["users"].most_common(3)) or "?"
        severity = "crit" if s["count"] >= SSH_CRIT else "warn" if _is_external(ip) else "info"
        where = "external" if _is_external(ip) else "local network"
        sec.add(severity, f"{ip} ×{s['count']} // {users}", f"failed logins from {where}")
    breached = 0
    for login in accepted:
        failed = sources.get(login["ip"])
        if failed and failed["first"] <= login["ts"]:
            breached += 1
            sec.add(
                "crit",
                f"ACCEPTED {clean(login['user'], 24)}@{login['ip']} AFTER {failed['count']} FAILURES",
                f"{login['method']} login succeeded after failed attempts from the same address",
            )
        elif len(accepted) <= 5:
            sec.add("info", f"ACCEPTED {clean(login['user'], 24)}@{login['ip']}", login["method"])
    if total >= SSH_CRIT:
        sec.raise_to("crit")
    elif external:
        sec.raise_to("warn")
    if total:
        sec.summary = (
            f"{total} FAILED SSH LOGIN{'S' if total > 1 else ''} // {len(sources)} "
            f"SOURCE{'S' if len(sources) > 1 else ''}"
        )
    else:
        sec.summary = "NO FAILED LOGINS"
    if breached:
        sec.summary = (
            f"{breached} LOGIN{'S' if breached > 1 else ''} AFTER FAILURES // " + sec.summary
        )
    if active is not None:
        sec.summary += " // SSHD " + ("ACTIVE" if active else "INACTIVE")
    sec.data = {
        "source": source,
        "sshd_active": active,
        "failed": total,
        "external": external,
        "sources": [
            {
                "ip": ip,
                "count": s["count"],
                "external": _is_external(ip),
                "users": [clean(u, 24) for u, _ in s["users"].most_common(5)],
            }
            for ip, s in ranked[:20]
        ],
        "accepted": [
            {
                "user": clean(a["user"], 24),
                "ip": a["ip"],
                "method": clean(a["method"], 30),
                "ts": a["ts"],
            }
            for a in accepted[-10:]
        ],
    }
    return sec


# 9. Vulnerabilities -----------------------------------------------------------------------------

SEVERITY = {"high": "crit", "medium": "warn", "low": "info", "unknown": "info"}
SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
DEBSECAN_LINE = re.compile(r"^((?:CVE|TEMP)-\S+)\s+(\S+)(?:\s+\((.*)\))?")


def debsecan_suite(distro: dict[str, str]) -> str | None:
    """The Debian suite to compare with. Kali rolling follows Debian testing, which takes its
    fixes from unstable, so Kali compares with sid: a fix there is on its way to Kali."""
    if distro.get("ID") == "kali":
        return "sid"
    if distro.get("ID") == "debian":
        return distro.get("VERSION_CODENAME") or "sid"
    return None


def parse_debsecan(out: str) -> list[dict[str, Any]]:
    """``debsecan`` lines, ``CVE-2024-1 openssl (fixed, remotely exploitable, high urgency)``,
    merged per package with its highest urgency."""
    merged: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        if not (m := DEBSECAN_LINE.match(line.strip())):
            continue
        notes = (m.group(3) or "").lower()
        severity = (
            "high" if "high urgency" in notes else "medium" if "medium urgency" in notes else "low"
        )
        item = merged.setdefault(
            m.group(2),
            {"package": clean(m.group(2), 60), "cves": [], "severity": severity, "remote": False},
        )
        item["cves"] = list(dict.fromkeys([*item["cves"], clean(m.group(1), 30)]))[:20]
        item["remote"] = item["remote"] or "remotely exploitable" in notes
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[item["severity"]]:
            item["severity"] = severity
    return list(merged.values())


@collector(
    "vulns",
    "VULNS",
    "Known vulnerabilities with a fix in Debian, in installed packages (debsecan)",
    timeout=120,
)
async def vulns(ctx: Context) -> Section:
    sec = Section("vulns", "VULNS")
    if not ctx.linux:
        return sec.unavailable("VULNERABILITY CHECK NEEDS LINUX")
    suite = debsecan_suite(ctx.distro())
    if suite is None:
        return sec.unavailable("NEEDS KALI OR DEBIAN (debsecan)")
    if not ctx.has("debsecan"):
        return sec.unavailable("INSTALL debsecan // sudo apt install debsecan")
    # Only what has a fix: on a rolling system the unfixed low-urgency list runs to hundreds.
    code, out, err = await ctx.run(["debsecan", "--suite", suite, "--only-fixed"], 100)
    if code != 0:
        return sec.unavailable(_why(code, err, "debsecan"))
    found = parse_debsecan(out)
    upgradable: set[str] = set()
    if found and ctx.has("apt"):
        code, out, _ = await ctx.run(["apt", "list", "--upgradable"], 60)
        if code == 0:
            upgradable = {u["name"] for u in _apt_updates(out)}
    for v in found:
        v["upgrade"] = v["package"] in upgradable
    found.sort(key=lambda v: (-SEVERITY_ORDER[v["severity"]], not v["upgrade"], v["package"]))
    kali = suite == "sid" and ctx.distro().get("ID") == "kali"
    for v in found[:15]:
        cves = v["cves"][0] + (f" +{len(v['cves']) - 1}" if len(v["cves"]) > 1 else "")
        detail = " // ".join(
            x
            for x in (
                f"{v['severity'].upper()} URGENCY",
                "remotely exploitable" if v["remote"] else "",
                "upgrade available: sudo apt full-upgrade"
                if v["upgrade"]
                else f"fixed in Debian {suite}, not in {'Kali' if kali else 'your repositories'} yet",
            )
            if x
        )
        sec.add(SEVERITY[v["severity"]], f"{v['package']} // {cves}", detail)
    if len(found) > 15:
        sec.add("info", f"+{len(found) - 15} MORE PACKAGES")
    high = sum(1 for v in found if v["severity"] == "high")
    ready = sum(1 for v in found if v["upgrade"])
    if not found:
        sec.summary = "NO FIXED VULNERABILITIES PENDING"
    else:
        sec.summary = f"{len(found)} VULNERABLE PACKAGE{'S' if len(found) > 1 else ''}"
        if high:
            sec.summary += f" // {high} HIGH"
        sec.summary += f" // {ready} UPGRADABLE NOW"
    sec.data.update(
        {"source": "debsecan", "suite": suite, "count": len(found), "high": high,
         "upgradable": ready, "packages": found[:40]}
    )  # fmt: skip
    return sec
