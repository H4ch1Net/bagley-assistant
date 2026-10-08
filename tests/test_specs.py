from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bagley import specs
from bagley.tools.system import system_specs

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(sys.platform != "linux", reason="reads a Linux file layout"),
]

LSPCI = """00:02.0 "VGA compatible controller" "Intel Corporation" "TigerLake-LP GT2 [Iris Xe Graphics]" -r01 "Dell" "Device 0a5d"
01:00.0 "3D controller" "NVIDIA Corporation" "GA107M [GeForce RTX 3050 Mobile]" -ra1 "Dell" "Device 0a5d"
00:1f.3 "Audio device" "Intel Corporation" "Tiger Lake-LP Smart Sound Technology Audio Controller" "Dell" "Device 0a5d\""""
LSBLK = {
    "blockdevices": [
        {"name": "nvme0n1", "size": 512110190592, "model": "Samsung SSD 980 PRO 512GB", "rota": False, "tran": "nvme", "type": "disk"},
        {"name": "sda", "size": 0, "model": "Card Reader", "rota": True, "tran": "usb", "type": "disk"},
        {"name": "loop0", "size": 4096000000, "model": None, "rota": False, "tran": None, "type": "loop"},
    ]
}  # fmt: skip
HYPR = [{"name": "eDP-1", "width": 1920, "height": 1200, "refreshRate": 59.95}]


OUTPUTS = {
    "lspci": LSPCI,
    "lsblk": json.dumps(LSBLK),
    "hyprctl": json.dumps(HYPR),
    "nvidia-smi": "NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 550.120",
}


class FakeLaptop(specs.Probe):
    def run(self, *cmd: str) -> str:
        return OUTPUTS.get(cmd[0], "")


@pytest.fixture
def laptop(tmp_path: Path) -> FakeLaptop:
    files = {
        "sys/class/dmi/id/sys_vendor": "Dell Inc.",
        "sys/class/dmi/id/product_name": "XPS 15 9510",
        "sys/class/dmi/id/product_version": "Not Specified",
        "etc/os-release": 'PRETTY_NAME="Kali GNU/Linux Rolling"\nNAME="Kali GNU/Linux"\nID=kali\n',
        "proc/cpuinfo": "processor\t: 0\nmodel name\t: 11th Gen Intel(R) Core(TM) i7-11800H @ 2.30GHz\n",
        "sys/class/power_supply/BAT0/energy_full": "73000000",
        "sys/class/power_supply/BAT0/energy_full_design": "86000000",
        "sys/class/power_supply/BAT0/cycle_count": "212",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return FakeLaptop(root=tmp_path)


def test_spec_sheet_of_a_kali_laptop(laptop, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "Hyprland")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    s = specs.collect(laptop)
    assert s["machine"] == "Dell Inc. XPS 15 9510"
    assert s["os"]["name"] == "Kali GNU/Linux Rolling"
    assert s["cpu"]["model"] == "11th Gen Intel Core i7-11800H @ 2.30GHz"
    assert [g["name"] for g in s["gpus"]] == [
        "NVIDIA GeForce RTX 3050 Laptop GPU",
        "Intel Iris Xe Graphics",
    ]
    assert s["gpus"][0]["vram_gb"] == 4.0
    assert s["disks"] == [
        {"name": "nvme0n1", "kind": "NVMe", "size": "512 GB", "model": "Samsung SSD 980 PRO 512GB"}
    ]
    assert s["displays"] == ["eDP-1 1920x1200@60Hz"]
    assert s["battery"]["health_percent"] == 85
    assert s["battery"]["cycles"] == 212
    sheet = "\n".join(s["summary"])
    assert "MACHINE  Dell Inc. XPS 15 9510" in sheet
    assert "GPU      NVIDIA GeForce RTX 3050 Laptop GPU · 4.0 GB VRAM" in sheet
    assert "STORAGE  NVMe 512 GB Samsung SSD 980 PRO 512GB" in sheet
    assert "OS       Kali GNU/Linux Rolling · kernel" in sheet
    assert "DISPLAY  Hyprland wayland · eDP-1 1920x1200@60Hz" in sheet


def test_missing_tools_leave_lines_out(tmp_path):
    class Bare(specs.Probe):
        def run(self, *cmd: str) -> str:
            return ""

    s = specs.collect(Bare(root=tmp_path))
    assert s["gpus"] == [] and s["disks"] == [] and s["machine"] == ""
    assert any(line.startswith("RAM") for line in s["summary"])
    assert any(line.startswith("STORAGE") for line in s["summary"])  # Falls back to filesystems.


async def test_specs_tool_returns_the_sheet():
    result = await system_specs.func()
    assert result["summary"] and result["memory_gb"] > 0
