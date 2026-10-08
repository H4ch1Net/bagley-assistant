"""Tools for client IT work: the asset inventory, client health checks and ticket summaries.

The inventory and health checks live in :mod:`bagley.work`; these are the thin tool wrappers the
model calls. Adding or changing an asset needs the user's approval. The health check and ticket
summary only read, so they are safe, but both touch the user's own client data, so they are
marked as reading private data for unattended runs.
"""

from __future__ import annotations

from typing import Annotated, Any

from bagley import work
from bagley.automations import Kind, register_kind
from bagley.policy import changes_state, reads_private
from bagley.toolroute import register_group
from bagley.tools import ToolContext, ToolError, tool

register_group(
    "work",
    "client asset inventory, client health checks and ticket summaries",
    r"\b(client|asset|inventory|ticket|laptop of|server of|warranty|health check|uptime"
    r"|customer|summary for|summarise the ticket|summarize the ticket|ats)\b",
)

register_kind(Kind(name="health", run=work.run_health, min_interval=15 * 60, needs_prompt=False))


def _asset_public(asset: dict[str, Any]) -> dict[str, Any]:
    """An asset for the model: drop the bulky internal flags, keep the useful fields."""
    out = {k: v for k, v in asset.items() if k not in ("custom_checks", "status_summary")}
    out["status"] = asset["status"] or "UNCHECKED"
    if asset["status_summary"]:
        out["status_detail"] = asset["status_summary"]
    return out


@tool(category="work", summary="List assets")
def list_assets(
    ctx: ToolContext,
    client: Annotated[str, "Only this client's assets"] = "",
    query: Annotated[str, "Search name, hostname, IP, tags, owner or serial"] = "",
) -> dict[str, Any]:
    """List client assets from the inventory, optionally for one client or matching a search.
    Each asset includes its last known health status."""
    assets = work.list_assets(ctx.store, client or None, query or None)
    return {
        "count": len(assets),
        "clients": [c["client"] for c in work.clients(ctx.store)],
        "assets": [_asset_public(a) for a in assets],
    }


@tool(category="work", risk="confirm", summary="Add asset {name} for {client}")
def add_asset(
    ctx: ToolContext,
    client: Annotated[str, "Client the asset belongs to"],
    name: Annotated[str, "A name for the asset, e.g. srv01 or 'Reception laptop'"],
    kind: Annotated[
        str, "laptop, desktop, server, printer, switch, router, firewall, nas, phone, vm or other"
    ] = "other",
    hostname: Annotated[str, "DNS name or host name"] = "",
    ip: Annotated[str, "IP address"] = "",
    os: Annotated[str, "Operating system, e.g. 'Windows 11' or 'Ubuntu 22.04'"] = "",
    serial: Annotated[str, "Serial number or service tag"] = "",
    model: Annotated[str, "Hardware model"] = "",
    owner: Annotated[str, "Who uses it"] = "",
    location: Annotated[str, "Where it is"] = "",
    warranty_until: Annotated[str, "Warranty end date, YYYY-MM-DD"] = "",
    notes: Annotated[str, "Anything else worth recording"] = "",
    tags: Annotated[str, "Comma-separated tags"] = "",
) -> dict[str, Any]:
    """Record a new asset in the client inventory. The kind sets sensible default health checks
    (a server is pinged and its SSH/RDP port checked, a printer its ping and port 9100, and so
    on). Asks the user first."""
    data = {
        "client": client, "name": name, "kind": kind, "hostname": hostname, "ip": ip, "os": os,
        "serial": serial, "model": model, "owner": owner, "location": location,
        "warranty_until": warranty_until, "notes": notes, "tags": tags,
    }  # fmt: skip
    data = {k: v for k, v in data.items() if v != ""}
    try:
        asset = work.add_asset(ctx.store, data)
    except work.AssetError as exc:
        raise ToolError(str(exc)) from exc
    return {"added": True, "asset": _asset_public(asset)}


@tool(category="work", risk="confirm", summary="Update asset #{asset_id}")
def update_asset(
    ctx: ToolContext,
    asset_id: Annotated[int, "The asset's id"],
    fields: Annotated[
        dict, 'Fields to change, e.g. {"ip": "10.0.0.5", "warranty_until": "2027-01-31"}'
    ],
) -> dict[str, Any]:
    """Change fields of an asset in the inventory. Asks the user first."""
    if not isinstance(fields, dict) or not fields:
        raise ToolError("Give the fields to change as an object.")
    try:
        asset = work.update_asset(ctx.store, asset_id, fields)
    except work.AssetError as exc:
        raise ToolError(str(exc)) from exc
    if asset is None:
        raise ToolError(f"There is no asset #{asset_id}.")
    return {"updated": True, "asset": _asset_public(asset)}


@tool(category="work", risk="confirm", summary="Remove asset #{asset_id}")
def remove_asset(
    ctx: ToolContext,
    asset_id: Annotated[int, "The asset's id"],
) -> str:
    """Delete an asset from the inventory. Asks the user first."""
    if not work.remove_asset(ctx.store, asset_id):
        raise ToolError(f"There is no asset #{asset_id}.")
    return f"Removed asset #{asset_id}."


@tool(category="work", summary="Check health for {client}", timeout=180.0)
async def check_client_health(
    ctx: ToolContext,
    client: Annotated[str, "Client to check (leave empty for all clients)"] = "",
) -> dict[str, Any]:
    """Run the health checks of a client's assets (ping, TCP, HTTP and TLS) and report OK, WARN,
    CRIT or UNKNOWN for each, with a summary and any warranties ending soon. Only reaches
    addresses recorded in the inventory. Reads only; it changes nothing."""
    if ctx.runtime is None:
        raise ToolError("Health checks need the running assistant.")
    result = await work.check_assets(ctx.runtime, client or None)
    if not result["assets"] and not result["findings"]:
        where = f" for {client}" if client else ""
        raise ToolError(f"There are no assets with checks to run{where}.")
    return {
        "summary": result["summary"],
        "report": result["report"],
        "clients": result["clients"],
        "changes": result["changes"],
        "findings": result["findings"],
        "assets": [
            {
                "name": a["name"],
                "client": a["client"],
                "status": a["status"],
                "detail": a["summary"],
            }
            for a in result["assets"]
        ],
    }


@tool(category="work", summary="Summarise ticket for {client}", timeout=180.0)
async def summarize_ticket(
    ctx: ToolContext,
    notes: Annotated[str, "The raw ticket notes to summarise"],
    client: Annotated[str, "The client the ticket is for"] = "",
) -> str:
    """Turn raw ticket notes into a short, client-ready summary in each of the work languages
    (English and French by default): what was reported, what was done, the result and any next
    steps, with internal detail and secrets left out. Returns the bilingual Markdown."""
    if ctx.runtime is None:
        raise ToolError("Ticket summaries need the running assistant.")
    try:
        result = await work.ticket_summary(ctx.runtime, notes, client or None)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return result["markdown"]


# Health checks and ticket summaries read the user's own client data; mark them so an
# unattended run won't use them after it has pulled in untrusted web content. Adding or
# changing assets changes saved state, so unattended runs can't do it (also risk=confirm).
reads_private("list_assets", "check_client_health", "summarize_ticket")
changes_state("add_asset", "update_asset", "remove_asset")
