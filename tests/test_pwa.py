from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bagley.assets import build_id, fingerprints
from bagley.server import create_app

STATIC = Path(__file__).resolve().parents[1] / "bagley" / "static"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def on_disk(url: str) -> Path:
    assert url.startswith("/static/"), url
    return STATIC / url.removeprefix("/static/")


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == PNG_SIGNATURE and head[12:16] == b"IHDR", f"{path.name} is not a PNG"
    return struct.unpack(">II", head[16:24])


@pytest.fixture
def client(make_runtime):
    with TestClient(create_app(make_runtime()), base_url="http://localhost") as c:
        yield c


def test_manifest():
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"] == manifest["short_name"] == "Bagley"
    assert manifest["display"] == "standalone" and manifest["scope"] == "/"
    assert manifest["start_url"] == "/?source=pwa"
    assert manifest["background_color"] == manifest["theme_color"] == "#0E0E0E"
    assert manifest["categories"] == ["productivity", "utilities"]
    purposes = {icon["purpose"] for icon in manifest["icons"]}
    assert purposes == {"any", "maskable"}
    for icon in manifest["icons"]:
        path = on_disk(icon["src"])
        assert path.is_file(), icon["src"]
        if icon["type"] == "image/png":
            width, height = (int(n) for n in icon["sizes"].split("x"))
            assert png_size(path) == (width, height)
    assert [(s["name"], s["url"]) for s in manifest["shortcuts"]] == [
        ("New chat", "/#/"),
        ("Approvals", "/#/approvals"),
    ]
    for shortcut in manifest["shortcuts"]:
        assert all(on_disk(icon["src"]).is_file() for icon in shortcut["icons"])


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("maskable-512.png", 512),
        ("apple-touch-icon.png", 180),
    ],
)
def test_icons_are_pngs_of_the_right_size(name, size):
    assert png_size(STATIC / "icons" / name) == (size, size)


def test_service_worker_is_served_from_the_root(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    assert resp.headers["service-worker-allowed"] == "/"
    assert resp.headers["cache-control"] == "no-cache"
    assert f'const VERSION = "{build_id()}"' in resp.text and "{{version}}" not in resp.text
    for needle in ("skipWaiting", "clients.claim", "notificationclick", '"/api/"', "caches.delete"):
        assert needle in resp.text
    assert client.head("/sw.js").status_code == 200

    static = client.get("/static/sw.js")
    assert static.status_code == 200
    assert "javascript" in static.headers["content-type"]
    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")


def test_service_worker_precaches_files_that_exist():
    source = (STATIC / "sw.js").read_text(encoding="utf-8")
    shell = re.search(r"const SHELL = \[(.*?)\];", source, re.S)
    assert shell
    urls = re.findall(r'"([^"]+)"', shell.group(1))
    assert "/" in urls and "/static/css/app.css" in urls
    for url in urls:
        if url != "/":
            assert on_disk(url).is_file(), f"sw.js precaches {url}, which doesn't exist"
    modules = {f"/static/js/{p.name}" for p in (STATIC / "js").glob("*.js")}
    assert modules <= set(urls), "add new modules to SHELL in sw.js"


def test_page_pins_every_asset_to_its_content(client):
    """An upgrade must never mix a cached old stylesheet or module into the new page."""
    resp = client.get("/")
    html = resp.text
    assert "{{" not in html
    prints = fingerprints()
    assert f'href="/static/css/app.css?v={prints["/static/css/app.css"]}"' in html
    assert f'src="/static/js/main.js?v={prints["/static/js/main.js"]}"' in html
    imports = json.loads(re.search(r'<script type="importmap">(.*?)</script>', html).group(1))[
        "imports"
    ]
    modules = sorted(p.relative_to(STATIC).as_posix() for p in (STATIC / "js").rglob("*.js"))
    for module in modules:
        url = f"/static/{module}"
        assert imports[url] == f"{url}?v={prints[url]}"
    body = re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in resp.headers["content-security-policy"]
