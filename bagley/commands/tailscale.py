"""``bagley tailscale``: reach Bagley from the phone and the laptop over the tailnet.

Reads ``tailscale status --json`` and prints the steps for HTTPS access through
``tailscale serve``, with the env lines Bagley needs. ``--apply`` writes those lines to
``~/.config/bagley/env``; ``--peers`` looks for Bagley on the other machines.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex

from bagley import service, tailscale
from bagley.config import ServerConfig
from bagley.readout import Readout


def cmd_tailscale(args: argparse.Namespace) -> int:
    out = Readout()
    port = getattr(args, "port", None) or ServerConfig.from_env().port
    try:
        net = tailscale.read_status(service.run_command)
    except tailscale.TailscaleError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            out.row("TAILSCALE", str(exc), "CRIT")
        return 1
    current = service.read_env()
    lines = tailscale.env_lines(net, current) if net.dns_name else {}
    serve = tailscale.serve_command(port)
    proxy = tailscale.serving(net, service.run_command) if net.running else ""
    found = asyncio.run(tailscale.find_bagleys(net.peers, port)) if args.peers else {}
    if args.apply and lines:
        service.set_env(lines)
    if args.json:
        body = {**net.to_dict(), "serve": serve, "serving": proxy, "env": lines, "bagleys": found}
        print(json.dumps(body))
        return 0 if net.running else 1

    out.row("TAILNET", f"{net.suffix or '--N/A--'}  {net.backend_state.upper()}")
    out.row("NODE", f"{net.machine.upper()}  {net.dns_name or '--N/A--'}  {' '.join(net.ips[:1])}")
    out.row("USER", net.login or "--N/A--")
    if net.peers:
        out.print()
        out.head("Peers")
        for peer in net.peers:
            state = out.paint("ok", "ONLINE ") if peer.online else out.paint("gray", "OFFLINE")
            ip = (peer.ips or ["--N/A--"])[0]
            line = f"  {peer.name.upper():<18} {peer.os.upper():<9} {ip:<16} {state}"
            if peer.name in found:
                line += f"  BAGLEY {found[peer.name]}"
            out.print(line)
    out.print()
    if not net.running:
        out.row("LINK", "TAILSCALE IS NOT CONNECTED: run `tailscale up`", "CRIT")
        return 1
    if not net.dns_name or not net.magic_dns:
        out.row("DNS", "TURN ON MAGICDNS IN THE ADMIN CONSOLE (DNS PAGE)", "WARN")
        return 1

    out.head("Phone access over HTTPS")
    out.note("  1. Admin console > DNS: MagicDNS and HTTPS Certificates on.")
    out.note("  2. Publish Bagley to the tailnet (not the internet):")
    out.cmd(" ".join(shlex.quote(a) for a in serve))
    if proxy:
        out.row("  SERVING", proxy, "OK" if proxy.endswith(f":{port}") else "WARN")
    else:
        out.note("     Access denied? Once: sudo tailscale set --operator=$USER")
    out.note("  3. Allow the name and give notifications a link that opens here:")
    for key, value in lines.items():
        done = current.get(key) == value or args.apply
        out.cmd(f"{key}={value}" + (out.paint("ok", "  [OK]") if done else ""))
    out.note("     Optional: only these tailnet users get in without the token:")
    out.cmd(f"BAGLEY_TAILSCALE_USERS={net.login or 'you@example.com'}")
    out.note("  4. Restart: systemctl --user restart bagley")
    out.note(f"  5. On the phone open {net.url} in Chrome and install the app.")
    out.note(f"     Laptop CLI and overlay: BAGLEY_URL={net.url}")
    out.print()
    if args.apply:
        out.row("APPLIED", f"{', '.join(lines)} > {service.env_path()}", "OK")
    else:
        out.note("  --apply writes the two lines of step 3 to ~/.config/bagley/env.")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("tailscale", help="Reach Bagley from the phone and laptop over Tailscale")
    p.add_argument("--apply", action="store_true", help="Write the env lines to the env file")
    p.add_argument("--peers", action="store_true", help="Look for Bagley on the other machines")
    p.add_argument("--port", type=int, default=argparse.SUPPRESS, help="Bagley's port")
    p.add_argument("--json", action="store_true", help="As JSON")
    p.set_defaults(func=cmd_tailscale)
