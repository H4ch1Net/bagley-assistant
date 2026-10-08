"""This machine on the tailnet: what ``tailscale status --json`` says, and how the phone and the
laptop reach Bagley here over HTTPS.

``tailscale serve --bg --https=443 http://127.0.0.1:<port>`` publishes the loopback port at
``https://<machine>.<tailnet>.ts.net`` to the tailnet only, with a certificate, and tells Bagley
who is calling in the ``Tailscale-User-Login`` header. Bagley keeps listening on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from bagley.service import Run, run_command

BAGLEY_DENIALS = ("Unauthorized. Open the URL printed by `bagley`", "This tailnet user is not")


class TailscaleError(Exception):
    pass


@dataclass
class Peer:
    name: str
    dns_name: str
    os: str
    ips: list[str]
    online: bool


@dataclass
class Tailnet:
    backend_state: str
    machine: str  # This node's host name.
    dns_name: str  # e.g. surface.tail1234.ts.net, without the trailing dot.
    ips: list[str]
    suffix: str  # The MagicDNS suffix, e.g. tail1234.ts.net.
    magic_dns: bool
    login: str  # The tailnet user this node belongs to.
    version: str = ""
    peers: list[Peer] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.backend_state == "Running"

    @property
    def url(self) -> str:
        return f"https://{self.dns_name}" if self.dns_name else ""

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "running": self.running, "url": self.url}


def _dns(name: Any) -> str:
    return str(name or "").rstrip(".").lower()


def parse_status(data: dict[str, Any]) -> Tailnet:
    """Pick what Bagley needs out of ``tailscale status --json``."""
    me = data.get("Self") or {}
    tailnet = data.get("CurrentTailnet") or {}
    users = data.get("User") or {}
    login = (users.get(str(me.get("UserID"))) or {}).get("LoginName", "")
    peers = [
        Peer(
            name=str(p.get("HostName") or _dns(p.get("DNSName")).split(".")[0]),
            dns_name=_dns(p.get("DNSName")),
            os=str(p.get("OS") or ""),
            ips=[str(ip) for ip in p.get("TailscaleIPs") or []],
            online=bool(p.get("Online")),
        )
        for p in (data.get("Peer") or {}).values()
        if isinstance(p, dict)
    ]
    return Tailnet(
        backend_state=str(data.get("BackendState") or "Unknown"),
        machine=str(me.get("HostName") or ""),
        dns_name=_dns(me.get("DNSName")),
        ips=[str(ip) for ip in me.get("TailscaleIPs") or data.get("TailscaleIPs") or []],
        suffix=_dns(data.get("MagicDNSSuffix") or tailnet.get("MagicDNSSuffix")),
        magic_dns=bool(tailnet.get("MagicDNSEnabled", bool(data.get("MagicDNSSuffix")))),
        login=str(login),
        version=str(data.get("Version") or ""),
        peers=sorted(peers, key=lambda p: p.name.lower()),
    )


def read_status(run: Run = run_command) -> Tailnet:
    code, out = run(["tailscale", "status", "--json"])
    if code == 127:
        raise TailscaleError("Tailscale is not installed (https://tailscale.com/download).")
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise TailscaleError(f"tailscale status failed: {out.strip()[:200]}") from exc
    if not isinstance(data, dict):
        raise TailscaleError("tailscale status returned something unexpected.")
    return parse_status(data)


def serve_command(port: int) -> list[str]:
    return ["tailscale", "serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"]


def serving(net: Tailnet, run: Run = run_command) -> str:
    """Where ``tailscale serve`` sends ``https://<this node>/`` now, or "" if nowhere."""
    code, out = run(["tailscale", "serve", "status", "--json"])
    try:
        data = json.loads(out) if code == 0 else {}
    except ValueError:
        return ""
    web = (data or {}).get("Web") or {}
    site = web.get(f"{net.dns_name}:443") or {}
    handler = (site.get("Handlers") or {}).get("/") or {}
    return str(handler.get("Proxy") or "")


def env_lines(net: Tailnet, current: dict[str, str] | None = None) -> dict[str, str]:
    """``BAGLEY_ALLOWED_HOSTS`` (merged with hosts already allowed) and ``BAGLEY_PUBLIC_URL``."""
    hosts = [h.strip() for h in (current or {}).get("BAGLEY_ALLOWED_HOSTS", "").split(",")]
    hosts = [h for h in hosts if h and h.lower() != net.dns_name] + [net.dns_name]
    return {"BAGLEY_ALLOWED_HOSTS": ",".join(hosts), "BAGLEY_PUBLIC_URL": net.url}


def looks_like_bagley(resp: httpx.Response) -> bool:
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            return False
        return isinstance(body, dict) and "state" in body and "code" in body
    return resp.status_code in (401, 403) and resp.text.startswith(BAGLEY_DENIALS)


async def find_bagleys(
    peers: list[Peer],
    port: int,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 1.0,
) -> dict[str, str]:
    """Online peers where Bagley answers, by name: on ``http://<ip>:<port>`` or behind
    ``tailscale serve`` at ``https://<name>``."""
    online = [p for p in peers if p.online]

    async def probe(http: httpx.AsyncClient, peer: Peer) -> str:
        bases = [f"http://{ip}:{port}" for ip in peer.ips if ":" not in ip][:1]
        if peer.dns_name:
            bases.append(f"https://{peer.dns_name}")
        for base in bases:
            try:
                resp = await http.get(f"{base}/api/activity")
            except httpx.HTTPError:
                continue
            if looks_like_bagley(resp):
                return base
        return ""

    async with httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False
    ) as http:
        found = await asyncio.gather(*(probe(http, p) for p in online))
    return {p.name: url for p, url in zip(online, found, strict=True) if url}
