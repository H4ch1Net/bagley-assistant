"""What this computer is: machine, CPU, GPU, memory, storage, OS, displays and battery health.

Live load (CPU %, memory in use, busy processes) is ``system_status``; this is the spec sheet.
Every probe is best effort: a missing tool or file leaves its line out instead of failing.
``Probe`` reads files and runs commands, so tests can hand it a fake machine.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

GIB = 1024**3
VENDORS = {
    "intel": "Intel",
    "advanced micro devices": "AMD",
    "amd": "AMD",
    "nvidia": "NVIDIA",
    "qualcomm": "Qualcomm",
    "apple": "Apple",
    "vmware": "VMware",
    "red hat": "Red Hat",
    "virtualbox": "VirtualBox",
    "innotek": "VirtualBox",
}
PCI_VENDOR_IDS = {"0x8086": "Intel", "0x1002": "AMD", "0x10de": "NVIDIA", "0x15ad": "VMware"}
NOISE = re.compile(r"\((?:R|TM|tm|r)\)|\bCPU\b|\bProcessor\b|\s+with Radeon Graphics")
FILLER = {"", "to be filled by o.e.m.", "default string", "system product name", "none", "n/a"}


@dataclass
class Probe:
    root: Path = Path("/")
    timeout: float = 3.0

    def read(self, path: str) -> str:
        try:
            return (self.root / path.lstrip("/")).read_text(errors="replace").strip()
        except OSError:
            return ""

    def glob(self, pattern: str) -> list[Path]:
        return sorted(self.root.glob(pattern.lstrip("/")))

    def run(self, *cmd: str) -> str:
        if not shutil.which(cmd[0]):
            return ""
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""


def _gb(n: float) -> float:
    return round(n / GIB, 1)


def _size(n: float) -> str:
    """Disk sizes the way they are sold: 512 GB, 1 TB."""
    gb = n / 1000**3
    return f"{gb / 1000:.1f} TB".replace(".0 TB", " TB") if gb >= 1000 else f"{round(gb)} GB"


def _vendor(name: str) -> str:
    low = name.lower()
    for key, short in VENDORS.items():
        if low.startswith(key):
            return short
    return name.split(",")[0].strip()


def _clean(text: str) -> str:
    return " ".join(NOISE.sub(" ", text).split())


def machine(p: Probe) -> str:
    if sys.platform == "darwin":
        return p.run("sysctl", "-n", "hw.model")
    parts = [p.read("/sys/class/dmi/id/sys_vendor"), p.read("/sys/class/dmi/id/product_name")]
    version = p.read("/sys/class/dmi/id/product_version")
    if version and not version.lower().startswith(("not ", "none")) and len(version) < 24:
        parts.append(version)
    parts = [x for x in parts if x.lower() not in FILLER]
    if not parts:
        model = p.read("/proc/device-tree/model").rstrip(
            "\x00"
        )  # Raspberry Pi and other ARM boards.
        return model
    if len(parts) > 1 and parts[1].lower().startswith(parts[0].lower()):
        parts = parts[1:]
    return " ".join(dict.fromkeys(parts))


def operating_system(p: Probe) -> dict[str, str]:
    info = {"kernel": platform.release(), "arch": platform.machine()}
    if sys.platform == "darwin":
        info["name"] = f"macOS {platform.mac_ver()[0]}".strip()
    elif sys.platform == "win32":
        info["name"] = f"Windows {platform.release()} ({platform.version()})"
    else:
        release = dict(
            re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', p.read("/etc/os-release"), flags=re.M)
        )
        info["name"] = release.get("PRETTY_NAME") or release.get("NAME") or platform.system()
    return info


def cpu(p: Probe) -> dict[str, Any]:
    name = ""
    if sys.platform == "darwin":
        name = p.run("sysctl", "-n", "machdep.cpu.brand_string")
    elif sys.platform != "win32":
        info = p.read("/proc/cpuinfo")
        for key in ("model name", "Hardware", "Model", "cpu model"):
            if m := re.search(rf"^{key}\s*:\s*(.+)$", info, flags=re.M):
                name = m.group(1)
                break
        if not name:
            name = p.run("lscpu").partition("Model name:")[2].split("\n")[0]
    name = _clean(name or platform.processor() or "")
    out: dict[str, Any] = {
        "model": name,
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(),
    }
    try:
        freq = psutil.cpu_freq()
    except (OSError, NotImplementedError, AttributeError):
        freq = None
    if freq and freq.max:
        out["max_ghz"] = round(freq.max / 1000, 1)
    return out


def _gpu_name(device: str) -> str:
    if m := re.search(r"\[([^\]]+)\]\s*$", device):  # "TigerLake-LP GT2 [Iris Xe Graphics]"
        return m.group(1)
    return device


def gpus(p: Probe) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    nvidia = p.run(
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    )
    for line in nvidia.splitlines():
        name, _, rest = line.partition(",")
        mem, _, driver = rest.partition(",")
        gpu: dict[str, Any] = {"name": name.strip(), "vendor": "NVIDIA"}
        if mem.strip().isdigit():
            gpu["vram_gb"] = round(int(mem) / 1024, 1)
        if driver.strip():
            gpu["driver"] = driver.strip()
        found.append(gpu)
    for line in p.run("lspci", "-mm").splitlines():
        fields = re.findall(r'"([^"]*)"', line)
        if len(fields) < 3 or not re.search(r"VGA|3D|Display", fields[0]):
            continue
        vendor = _vendor(fields[1])
        if vendor == "NVIDIA" and nvidia:
            continue  # nvidia-smi already named it, with its memory.
        name = _gpu_name(fields[2])
        found.append(
            {"name": name if name.startswith(vendor) else f"{vendor} {name}", "vendor": vendor}
        )
    if not found:
        for card in p.glob("/sys/class/drm/card[0-9]/device/vendor"):
            vendor = PCI_VENDOR_IDS.get(p.read(str(card.relative_to(p.root))).lower())
            if vendor:
                found.append({"name": f"{vendor} graphics", "vendor": vendor})
    if not found and sys.platform == "darwin":
        data = p.run("system_profiler", "SPDisplaysDataType", "-json")
        try:
            for item in json.loads(data).get("SPDisplaysDataType", []):
                found.append({"name": item.get("sppci_model", "GPU"), "vendor": "Apple"})
        except (ValueError, AttributeError):
            pass
    return found


def storage(p: Probe) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    raw = p.run("lsblk", "-J", "-d", "-b", "-o", "NAME,SIZE,MODEL,ROTA,TRAN,TYPE")
    try:
        devices = json.loads(raw).get("blockdevices", []) if raw else []
    except ValueError:
        devices = []
    for d in devices:
        name = d.get("name") or ""
        if d.get("type") != "disk" or name.startswith(("loop", "zram", "ram", "sr")):
            continue
        size = int(d.get("size") or 0)
        if size < 1000**3:  # Empty card readers and tiny virtual disks.
            continue
        rota = d.get("rota") in (True, "1", 1)
        if name.startswith("nvme") or d.get("tran") == "nvme":
            kind = "NVMe"
        elif name.startswith(("vd", "xvd")):
            kind = "Virtual disk"
        else:
            kind = "HDD" if rota else "SSD"
        if d.get("tran") == "usb":
            kind = f"USB {kind}"
        disk = {"name": name, "kind": kind, "size": _size(size)}
        if (d.get("model") or "").strip():
            disk["model"] = " ".join(d["model"].split())
        disks.append(disk)
    return disks


def filesystems() -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for path in ("/", str(Path.home())):
        try:
            usage = psutil.disk_usage(path)
        except OSError:
            continue
        key = f"{usage.total}:{usage.used}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"mount": path, "total_gb": _gb(usage.total), "free_gb": _gb(usage.free)})
    return out


def displays(p: Probe) -> list[str]:
    raw = p.run("hyprctl", "monitors", "-j")
    if raw:
        try:
            return [
                f"{m['name']} {m['width']}x{m['height']}@{round(m.get('refreshRate', 60))}Hz"
                for m in json.loads(raw)
            ]
        except (ValueError, KeyError, TypeError):
            pass
    xrandr = p.run("xrandr", "--current")
    return [
        f"{m.group(1)} {m.group(2)}"
        for m in re.finditer(r"^(\S+) connected (?:primary )?(\d+x\d+)\+", xrandr, flags=re.M)
    ]


def battery(p: Probe) -> dict[str, Any] | None:
    try:
        live = psutil.sensors_battery()
    except (AttributeError, NotImplementedError, OSError):
        live = None
    info: dict[str, Any] = {}
    if live is not None:
        info = {"percent": round(live.percent), "plugged_in": live.power_plugged}
    for bat in p.glob("/sys/class/power_supply/BAT*"):
        base = str(bat.relative_to(p.root))
        for full, design in (
            ("energy_full", "energy_full_design"),
            ("charge_full", "charge_full_design"),
        ):
            now, new = p.read(f"{base}/{full}"), p.read(f"{base}/{design}")
            if now.isdigit() and new.isdigit() and int(new):
                info["health_percent"] = min(100, round(100 * int(now) / int(new)))
                break
        cycles = p.read(f"{base}/cycle_count")
        if cycles.isdigit() and int(cycles):
            info["cycles"] = int(cycles)
        break
    return info or None


def collect(p: Probe | None = None) -> dict[str, Any]:
    p = p or Probe()
    vm, swap = psutil.virtual_memory(), psutil.swap_memory()
    specs: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "machine": machine(p),
        "os": operating_system(p),
        "cpu": cpu(p),
        "gpus": gpus(p),
        "memory_gb": _gb(vm.total),
        "swap_gb": _gb(swap.total),
        "disks": storage(p),
        "filesystems": filesystems(),
        "displays": displays(p),
        "desktop": " ".join(
            x
            for x in (
                os.environ.get("XDG_CURRENT_DESKTOP", ""),
                os.environ.get("XDG_SESSION_TYPE", ""),
            )
            if x
        ),
        "battery": battery(p),
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
    }
    specs["summary"] = summary(specs)
    return specs


def summary(s: dict[str, Any]) -> list[str]:
    """A fixed-width spec sheet, ready to show as is."""
    lines: list[tuple[str, str]] = []
    if s.get("machine"):
        lines.append(("MACHINE", s["machine"]))
    c = s.get("cpu") or {}
    if c.get("model"):
        bits = [c["model"]]
        if c.get("cores") and c.get("threads"):
            bits.append(f"{c['cores']} cores / {c['threads']} threads")
        if c.get("max_ghz"):
            bits.append(f"up to {c['max_ghz']} GHz")
        lines.append(("CPU", " · ".join(bits)))
    for g in s.get("gpus") or []:
        lines.append(
            ("GPU", g["name"] + (f" · {g['vram_gb']} GB VRAM" if g.get("vram_gb") else ""))
        )
    memory = f"{s['memory_gb']} GB"
    if s.get("swap_gb"):
        memory += f" · swap {s['swap_gb']} GB"
    lines.append(("RAM", memory))
    for d in s.get("disks") or []:
        lines.append(
            ("STORAGE", " ".join(x for x in (d["kind"], d["size"], d.get("model", "")) if x))
        )
    if not s.get("disks"):
        for f in s.get("filesystems") or []:
            lines.append(("STORAGE", f"{f['mount']} {f['total_gb']} GB, {f['free_gb']} GB free"))
    o = s.get("os") or {}
    lines.append(
        (
            "OS",
            " · ".join(
                x
                for x in (
                    o.get("name"),
                    f"kernel {o['kernel']}" if o.get("kernel") else "",
                    o.get("arch"),
                )
                if x
            ),
        )
    )
    if s.get("desktop") or s.get("displays"):
        lines.append(
            (
                "DISPLAY",
                " · ".join([s["desktop"], *s["displays"]] if s.get("desktop") else s["displays"]),
            )
        )
    b = s.get("battery")
    if b:
        bits = [f"{b['percent']}%"] if "percent" in b else []
        if "health_percent" in b:
            bits.append(f"health {b['health_percent']}%")
        if bits:
            lines.append(("BATTERY", " · ".join(bits)))
    return [f"{k:<8} {v}" for k, v in lines]
