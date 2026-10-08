"""The installable app: ``GET /sw.js`` serves the service worker from the site root, so its scope
can be the whole app (a worker under ``/static/`` could only control ``/static/``). The page
registers it with ``navigator.serviceWorker.register("/sw.js")``.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

from bagley.assets import build_id

router = APIRouter()

SW_FILE = Path(__file__).resolve().parent.parent / "static" / "sw.js"

# Python before 3.11 doesn't know the manifest type; the static files use this table.
mimetypes.add_type("application/manifest+json", ".webmanifest")


@router.api_route("/sw.js", methods=["GET", "HEAD"], include_in_schema=False)
async def service_worker() -> Response:
    # The build id names the cache, so any change to the UI replaces the cached app shell.
    text = SW_FILE.read_text(encoding="utf-8").replace("{{version}}", build_id())
    return Response(
        text,
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
            "X-Content-Type-Options": "nosniff",
        },
    )
