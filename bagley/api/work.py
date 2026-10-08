"""HTTP endpoints for the work feature: the asset inventory, health checks and ticket summaries.

* ``GET/POST /api/assets``, ``PATCH/DELETE /api/assets/{id}`` and ``GET /api/assets/clients``
  are inventory CRUD.
* ``POST /api/assets/import`` takes a CSV body; ``GET /api/assets/export`` returns one.
* ``POST /api/work/health`` runs the checks; ``GET /api/work/health/last`` returns the stored
  result of the last run.
* ``POST /api/work/ticket-summary`` returns the bilingual Markdown summary of ticket notes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from bagley import work
from bagley.api import runtime
from bagley.llm import LLMError

router = APIRouter()
MAX_CSV_BYTES = work.MAX_CSV_BYTES


class AssetBody(BaseModel):
    client: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="other", max_length=40)
    hostname: str = Field(default="", max_length=253)
    ip: str = Field(default="", max_length=45)
    os: str = Field(default="", max_length=80)
    serial: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=120)
    owner: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=120)
    warranty_until: str | None = Field(default=None, max_length=40)
    notes: str = Field(default="", max_length=4000)
    tags: list[str] | str = Field(default="")
    checks: list[dict[str, Any]] | None = None


class AssetPatch(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)


class HealthBody(BaseModel):
    client: str | None = Field(default=None, max_length=80)
    asset_ids: list[int] | None = None


class TicketBody(BaseModel):
    notes: str = Field(min_length=1, max_length=work.MAX_NOTES)
    client: str | None = Field(default=None, max_length=80)
    languages: list[str] | None = Field(default=None, max_length=4)


def _bad(exc: work.AssetError) -> HTTPException:
    return HTTPException(422, str(exc))


# Inventory --------------------------------------------------------------------------------------


@router.get("/api/assets")
async def get_assets(request: Request, client: str = "", q: str = "") -> dict[str, Any]:
    store = runtime(request).store
    assets = work.list_assets(store, client or None, q or None)
    return {"count": len(assets), "assets": assets}


@router.post("/api/assets", status_code=201)
async def create_asset(body: AssetBody, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    try:
        asset = work.add_asset(rt.store, body.model_dump(exclude_none=True))
    except work.AssetError as exc:
        raise _bad(exc) from exc
    await rt.broadcast({"type": "assets.changed"})
    return asset


@router.patch("/api/assets/{asset_id}")
async def patch_asset(asset_id: int, body: AssetPatch, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    try:
        asset = work.update_asset(rt.store, asset_id, body.fields)
    except work.AssetError as exc:
        raise _bad(exc) from exc
    if asset is None:
        raise HTTPException(404, f"No asset #{asset_id}.")
    await rt.broadcast({"type": "assets.changed"})
    return asset


@router.delete("/api/assets/{asset_id}", status_code=204)
async def delete_asset(asset_id: int, request: Request) -> None:
    rt = runtime(request)
    if not work.remove_asset(rt.store, asset_id):
        raise HTTPException(404, f"No asset #{asset_id}.")
    await rt.broadcast({"type": "assets.changed"})


@router.get("/api/assets/clients")
async def asset_clients(request: Request) -> dict[str, Any]:
    return {"clients": work.clients(runtime(request).store)}


@router.post("/api/assets/import")
async def import_assets(request: Request, client: str = "") -> dict[str, Any]:
    rt = runtime(request)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_CSV_BYTES:
            raise HTTPException(413, "CSV too large.")
    try:
        text = work.decode_csv(bytes(body))
        result = work.import_csv(rt.store, text, client or None)
    except work.AssetError as exc:
        raise _bad(exc) from exc
    await rt.broadcast({"type": "assets.changed"})
    return result


@router.get("/api/assets/export")
async def export_assets(request: Request, client: str = "") -> PlainTextResponse:
    csv_text = work.export_csv(runtime(request).store, client or None)
    name = f"assets-{client}.csv" if client else "assets.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# Health -----------------------------------------------------------------------------------------


@router.post("/api/work/health")
async def run_health(body: HealthBody, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    result = await work.check_assets(rt, body.client, body.asset_ids)
    await rt.broadcast({"type": "assets.changed"})
    return result


@router.get("/api/work/health/last")
async def last_health(request: Request, client: str = "") -> dict[str, Any]:
    return work.last_results(runtime(request).store, client or None)


# Ticket summaries -------------------------------------------------------------------------------


@router.post("/api/work/ticket-summary")
async def ticket_summary(body: TicketBody, request: Request) -> dict[str, Any]:
    rt = runtime(request)
    try:
        return await work.ticket_summary(rt, body.notes, body.client, body.languages)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, exc.message) from exc
