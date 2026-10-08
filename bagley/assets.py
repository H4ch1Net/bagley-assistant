"""Content fingerprints for the web UI's static files.

Asset URLs carry a hash of their content (``app.css?v=1a2b3c4d5e``) and every ES module import
is pinned the same way through an import map, so a browser can never keep a stale stylesheet or
module across an upgrade, however long it cached the old one.
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
MODULES = ("js", "vendor")


@functools.cache
def fingerprints(root: Path = STATIC_DIR) -> dict[str, str]:
    """``/static/<path>`` -> the first 10 hex digits of its SHA-256."""
    return {
        "/static/" + path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()[:10]
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


@functools.cache
def build_id(root: Path = STATIC_DIR) -> str:
    """One hash for the whole UI: changes whenever any file does."""
    digest = hashlib.sha256(json.dumps(fingerprints(root), sort_keys=True).encode())
    return digest.hexdigest()[:10]


def versioned(url: str, root: Path = STATIC_DIR) -> str:
    return f"{url}?v={fingerprints(root).get(url, build_id(root))}"


def import_map(root: Path = STATIC_DIR) -> str:
    """Maps each module's plain URL, as relative imports resolve it, to its versioned URL."""
    imports = {
        url: f"{url}?v={digest}"
        for url, digest in fingerprints(root).items()
        if url.endswith(".js") and url.split("/")[2] in MODULES
    }
    return json.dumps({"imports": imports}, separators=(",", ":"))


def render_page(html: str, root: Path = STATIC_DIR) -> str:
    """Fill the page's asset placeholders."""
    return (
        html.replace("{{importmap}}", import_map(root))
        .replace("{{css}}", versioned("/static/css/app.css", root))
        .replace("{{main}}", versioned("/static/js/main.js", root))
    )
