"""HTTP routers for feature modules.

Every module in this package that defines ``router`` (a FastAPI ``APIRouter``) is mounted by
``create_app``, so a feature adds endpoints without touching ``server.py``. Handlers reach the
shared state with ``runtime(request)``.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from fastapi import APIRouter
from starlette.requests import HTTPConnection

if TYPE_CHECKING:
    from bagley.runtime import Runtime


def runtime(conn: HTTPConnection) -> Runtime:
    """The app's runtime, for a request or a WebSocket."""
    return conn.app.state.runtime


def routers() -> list[APIRouter]:
    found: list[APIRouter] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            found.append(router)
    return found
