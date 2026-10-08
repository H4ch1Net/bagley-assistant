"""``GET /api/tailscale``: this machine on the tailnet and how the phone reaches it, for the
settings page. Read-only; ``bagley tailscale --apply`` writes the env lines."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from bagley import service, tailscale
from bagley.api import runtime

router = APIRouter()


@router.get("/api/tailscale")
async def tailnet(request: Request) -> dict[str, Any]:
    rt = runtime(request)
    config = rt.config
    try:
        net = await asyncio.to_thread(tailscale.read_status, service.run_command)
    except tailscale.TailscaleError as exc:
        return {"available": False, "error": str(exc)}
    proxy = await asyncio.to_thread(tailscale.serving, net, service.run_command)
    allowed = {h.lower() for h in config.allowed_hosts}
    public = (rt.env.get("BAGLEY_PUBLIC_URL") or "").rstrip("/")
    return {
        "available": True,
        **net.to_dict(),
        "serve": tailscale.serve_command(config.port),
        "serving": proxy,
        "env": tailscale.env_lines(net, {"BAGLEY_ALLOWED_HOSTS": ",".join(config.allowed_hosts)}),
        "host_allowed": bool(net.dns_name) and net.dns_name in allowed,
        "public_url_set": bool(net.url) and public == net.url,
        "identity_check": bool(config.tailscale_users),
        "token": bool(config.token),
    }
