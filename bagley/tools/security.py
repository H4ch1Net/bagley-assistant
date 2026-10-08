"""Tools for a defensive security analyst: CVE lookups, decoding, hashes, ports, TLS and logs.

Most of them only look at text the user pasted and never run or fetch anything. The two that
touch the network are careful about targets: ``scan_ports`` only checks private, loopback,
link-local and Tailscale addresses (or hosts in the work asset inventory) and asks first;
``tls_check`` reads certificates of public hosts, and of private ones only when they are in the
inventory.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import html
import ipaddress
import json
import quopri
import re
import socket
import ssl
import time
import weakref
import zlib
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import unquote, urlsplit

import httpx

from bagley import work
from bagley.policy import brings_web, reads_private
from bagley.toolroute import GROUPS, register_group
from bagley.tools import ToolContext, ToolError, tool

if "security" not in GROUPS:  # The machine security checks may have registered it already.
    register_group(
        "security",
        "security analysis: CVE lookups, decoding suspicious data, hashes, ports, TLS "
        "certificates and logs",
        r"\b(cve|vuln|exploit|secur|attack|malware|phish|brute|harden|ctf|threat|incident|ioc"
        r"|port|scan|nmap|hash|decod|deobfusc|base64|jwt|token|tls|ssl|cert|cipher|log|auth\.log"
        r"|ssh|firewall|intrusion|suspicious)",
    )

USER_AGENT = "BagleyAssistant/0.1 (security analyst tools)"


# Ports ------------------------------------------------------------------------------------------

# port: (service, what to watch for, how to harden it). TCP unless the name says udp.
PORTS: dict[int, tuple[str, str, str]] = {
    20: ("ftp-data", "FTP data channel; files travel in cleartext.", "Prefer SFTP or FTPS."),
    21: ("ftp", "Cleartext logins and anonymous access; old daemons.", "Use SFTP/FTPS, disable anonymous login, keep the server patched."),
    22: ("ssh", "Password guessing against exposed hosts; keep OpenSSH current.", "Keys only, no root login, fail2ban or an allowlist, patch OpenSSH."),
    23: ("telnet", "Everything in cleartext; default credentials on IoT gear.", "Disable it and use SSH."),
    25: ("smtp", "Open relay abuse and address enumeration (VRFY/EXPN).", "No open relay, STARTTLS and auth on submission, SPF/DKIM/DMARC."),
    53: ("dns", "Open resolver amplification and zone transfers that leak hosts.", "Restrict recursion to your networks, deny AXFR except to secondaries."),
    67: ("dhcp (udp)", "Rogue DHCP servers can redirect clients.", "DHCP snooping on switches; only expected servers."),
    69: ("tftp (udp)", "No authentication; often serves device configs.", "Disable, or restrict to the management network, read-only."),
    79: ("finger", "Leaks user names and login times.", "Disable."),
    80: ("http", "Cleartext web traffic and exposed admin pages.", "Redirect to HTTPS, patch the app, keep admin pages off the internet."),
    81: ("http-alt", "Often an IoT or camera admin page with default credentials.", "Change defaults, firewall it, update firmware."),
    88: ("kerberos", "Weak service-account passwords are a known target.", "Long random service passwords (gMSA), require pre-auth, AES only."),
    110: ("pop3", "Cleartext mail logins.", "Use POP3S (995) or disable."),
    111: ("rpcbind", "Lists RPC/NFS services; UDP amplification.", "Firewall it from untrusted networks."),
    113: ("ident", "Leaks which user owns a connection.", "Disable unless needed."),
    119: ("nntp", "Legacy news server with cleartext auth.", "Disable or use NNTPS."),
    123: ("ntp (udp)", "monlist amplification on old ntpd.", "Patch ntpd, disable monlist, restrict queries."),
    135: ("msrpc", "Windows RPC endpoint mapper used for remote management.", "Never expose to the internet; restrict to admin hosts."),
    137: ("netbios-ns (udp)", "Name-service poisoning can capture hashes.", "Disable NetBIOS over TCP/IP."),
    138: ("netbios-dgm (udp)", "Legacy browsing and information leaks.", "Disable NetBIOS over TCP/IP."),
    139: ("netbios-ssn", "SMB over NetBIOS: share enumeration and relay.", "Disable NetBIOS; firewall SMB."),
    143: ("imap", "Cleartext mail logins.", "Use IMAPS (993)."),
    161: ("snmp (udp)", "Default community strings leak or change configs.", "SNMPv3 with auth and privacy, or unique communities and ACLs."),
    162: ("snmp-trap (udp)", "Spoofed traps.", "Accept traps from known devices only."),
    179: ("bgp", "Session tampering and route injection.", "TCP-AO/MD5 auth, allow only known peers."),
    389: ("ldap", "Anonymous binds and cleartext credentials.", "Require LDAP signing and channel binding; prefer LDAPS."),
    427: ("slp", "VMware ESXi SLP has a history of serious bugs.", "Disable SLP on ESXi, patch, never expose it."),
    443: ("https", "Web app flaws, outdated TLS, exposed portals.", "Patch, TLS 1.2+, MFA on portals, a WAF where sensible."),
    445: ("smb", "File sharing; a common lateral-movement and relay path.", "Disable SMBv1, require signing, never expose to the internet."),
    464: ("kpasswd", "Kerberos password changes; part of AD.", "Keep inside the domain network."),
    465: ("smtps", "Authenticated mail submission; credential guessing.", "Strong passwords or app passwords, rate limits."),
    500: ("isakmp (udp)", "IKE aggressive mode can leak crackable PSK hashes.", "Main mode, certificates or long PSKs, IKEv2."),
    502: ("modbus", "Industrial control with no authentication.", "Isolate OT networks; never expose."),
    512: ("rexec", "Legacy cleartext remote execution.", "Disable."),
    513: ("rlogin", "Trust-based logins.", "Disable."),
    514: ("rsh / syslog (udp)", "rsh trust abuse; syslog spoofing.", "Disable rsh; accept syslog from known hosts, prefer TLS syslog."),
    515: ("lpd", "Printer queue with old LPD bugs.", "Restrict to clients that print."),
    520: ("rip (udp)", "Route injection.", "RIPv2 with auth, or disable."),
    548: ("afp", "Apple file sharing with old auth.", "Use SMB3 instead; firewall."),
    554: ("rtsp", "IP camera streams, often without auth.", "Require auth; isolate cameras on their own VLAN."),
    587: ("submission", "Mail submission; credential stuffing.", "STARTTLS, strong auth, rate limits."),
    593: ("http-rpc-epmap", "RPC over HTTP used for remote management.", "Firewall from untrusted networks."),
    623: ("ipmi (udp)", "BMC management plane with known auth weaknesses.", "Isolate BMCs on a management VLAN, change defaults."),
    631: ("ipp", "CUPS printing; keep cups-browsed off if unused.", "Disable cups-browsed, restrict to localhost."),
    636: ("ldaps", "LDAP over TLS; directory credential guessing.", "Account lockout, monitor binds."),
    853: ("dns-over-tls", "Encrypted DNS can bypass DNS filtering.", "Allow only approved resolvers if you filter DNS."),
    873: ("rsync", "Unauthenticated modules can expose or accept files.", "Require auth, read-only modules, firewall."),
    902: ("vmware-auth", "ESXi/Workstation console authentication.", "Management network only; patch ESXi."),
    990: ("ftps", "FTP over TLS; credential guessing.", "Strong passwords, IP allowlist."),
    993: ("imaps", "Mail logins; credential stuffing.", "App passwords or MFA, lockout."),
    995: ("pop3s", "Mail logins; credential stuffing.", "App passwords or MFA, or disable POP."),
    1080: ("socks", "Open proxies get abused to relay traffic.", "Require auth, bind to localhost or VPN."),
    1194: ("openvpn", "VPN endpoint; keep it current.", "Certificates plus MFA, keep patched."),
    1433: ("mssql", "Admin-account guessing and data exposure.", "Disable the sa account, no internet exposure, encrypt connections."),
    1434: ("mssql-browser (udp)", "Lists SQL instances; amplification.", "Disable when using fixed ports."),
    1521: ("oracle-tns", "Listener tampering and default accounts.", "Listener password, valid node checking, patch."),
    1701: ("l2tp (udp)", "Weak without IPsec.", "Use L2TP/IPsec or a modern VPN."),
    1723: ("pptp", "PPTP encryption is considered broken.", "Replace with WireGuard, IKEv2 or OpenVPN."),
    1812: ("radius (udp)", "Shared-secret auth with known protocol weaknesses.", "Message-Authenticator, RadSec, long secrets."),
    1883: ("mqtt", "Brokers without auth leak or accept IoT commands.", "Require auth and TLS (8883), ACLs per topic."),
    1900: ("ssdp/upnp (udp)", "Amplification; UPnP can open firewall ports.", "Disable UPnP on the router, block SSDP at the edge."),
    2049: ("nfs", "Over-broad exports and no_root_squash.", "Export to specific hosts, root_squash, NFSv4 with Kerberos."),
    2082: ("cpanel", "Hosting control panel logins.", "MFA, IP allowlist."),
    2083: ("cpanel-ssl", "Hosting control panel logins.", "MFA, IP allowlist."),
    2181: ("zookeeper", "Unauthenticated access to cluster data.", "Enable auth, bind to the cluster network."),
    2222: ("ssh-alt", "SSH on another port: same considerations as 22.", "Same hardening as SSH; the port change is not a control."),
    2375: ("docker", "Docker API without TLS grants host-level control.", "Never expose; use the socket or TLS with client certs (2376)."),
    2376: ("docker-tls", "Docker API with TLS; protect the client certs.", "Restrict by firewall, guard the certs."),
    2379: ("etcd", "Holds Kubernetes secrets; often unauthenticated.", "Client-cert auth, firewall to the control plane only."),
    3000: ("dev-http / grafana", "Dev servers and Grafana with default admin.", "Change defaults; don't expose dev servers."),
    3128: ("squid", "Open proxy abuse and internal network access.", "Require auth, ACLs."),
    3268: ("ldap-gc", "Active Directory global catalog queries.", "Internal only."),
    3306: ("mysql", "Credential guessing and data exposure.", "Bind to localhost or app hosts, strong passwords, TLS."),
    3389: ("rdp", "A common remote-access target; keep it patched.", "Behind a VPN or gateway, NLA, MFA, lockout."),
    3478: ("stun/turn", "Relay abuse to reach internal hosts.", "Auth on TURN, deny relaying to private ranges."),
    3690: ("svn", "Repositories readable without auth.", "Require auth, use HTTPS."),
    4369: ("epmd", "Erlang port mapper; cluster cookies.", "Firewall to cluster nodes."),
    4443: ("https-alt", "Admin or VPN portals.", "Patch, MFA, restrict by IP."),
    4500: ("ipsec-nat-t (udp)", "VPN endpoint.", "Keep patched, strong auth."),
    4786: ("cisco-smart-install", "Legacy feature that allows config changes.", "Turn off Smart Install; block at the edge."),
    5000: ("upnp / synology / dev-http", "Synology DSM, Flask dev servers, UPnP.", "Don't expose DSM directly; MFA; no dev servers on the LAN."),
    5001: ("synology-https", "Synology DSM admin.", "VPN for remote access, MFA, auto-block."),
    5060: ("sip", "SIP credential guessing and toll fraud.", "Strong extension passwords, fail2ban, allow only your provider."),
    5061: ("sips", "SIP over TLS; same concerns as 5060.", "As for SIP."),
    5353: ("mdns (udp)", "Leaks device and service names; amplification.", "Don't route mDNS between networks or to the internet."),
    5355: ("llmnr (udp)", "Name-service poisoning can capture hashes.", "Disable LLMNR by GPO."),
    5432: ("postgresql", "Credential guessing; loose pg_hba.conf trust.", "scram-sha-256, bind to app hosts, TLS."),
    5601: ("kibana", "Data exposure; keep it current.", "Enable Elastic security; never public."),
    5672: ("amqp", "RabbitMQ with the default guest account.", "Remove guest, TLS, firewall."),
    5900: ("vnc", "Weak or absent passwords, cleartext.", "Tunnel over SSH/VPN, strong passwords."),
    5985: ("winrm-http", "Remote PowerShell used for management.", "Admin hosts only; prefer HTTPS (5986)."),
    5986: ("winrm-https", "Remote PowerShell over TLS.", "Admin hosts only, JEA."),
    6000: ("x11", "Open displays can expose input.", "Never listen on TCP; use SSH forwarding."),
    6379: ("redis", "No auth by default.", "requirepass/ACLs, bind to localhost, protected-mode on."),
    6443: ("kubernetes-api", "Cluster control plane; guard tokens.", "Disable anonymous auth, RBAC, restrict by IP."),
    6667: ("irc", "Sometimes used by malware for control channels.", "Investigate unexpected IRC traffic."),
    7001: ("weblogic", "History of deserialisation bugs.", "Patch; never expose the admin console."),
    8000: ("http-alt", "Dev servers and admin panels.", "Don't expose dev servers; require auth."),
    8006: ("proxmox", "Proxmox VE web UI controls VMs.", "VPN only, 2FA, keep patched."),
    8008: ("http-alt", "Admin panels, Chromecast.", "Restrict to the LAN."),
    8009: ("ajp", "Tomcat AJP connector; a known file-read path.", "Disable AJP or require a secret."),
    8080: ("http-proxy / alt", "Tomcat manager, Jenkins, proxies with defaults.", "Auth, remove default apps, restrict."),
    8081: ("http-alt", "Admin panels, Nexus.", "Auth, restrict."),
    8086: ("influxdb", "Unauthenticated time-series data.", "Enable auth, bind to localhost."),
    8088: ("yarn / http-alt", "Hadoop YARN can accept jobs without auth.", "Enable Kerberos auth, firewall."),
    8123: ("home-assistant", "Home automation control.", "Strong auth with MFA; expose via VPN or Nabu Casa only."),
    8291: ("mikrotik-winbox", "RouterOS management; keep it updated.", "Update RouterOS, restrict to management."),
    8443: ("https-alt", "UniFi controllers, firewall and VPN admin pages.", "MFA, IP allowlist, patch."),
    8765: ("bagley", "This assistant's web server.", "Keep on loopback or Tailscale; set BAGLEY_TOKEN for remote access."),
    8888: ("http-alt / jupyter", "Jupyter without a token runs code.", "Require a token or password, bind to localhost."),
    9000: ("portainer / php-fpm / minio", "Container admin, exposed FastCGI.", "Require auth; bind php-fpm to a socket."),
    9042: ("cassandra", "Unauthenticated database access.", "Enable auth, firewall."),
    9090: ("prometheus / cockpit", "Metrics leak internal detail; Cockpit admin.", "Auth, restrict to monitoring hosts."),
    9100: ("jetdirect", "Raw printing and PJL file access.", "Restrict to print servers."),
    9200: ("elasticsearch", "Open clusters leak data.", "Enable security, bind to app hosts."),
    9418: ("git-daemon", "Unauthenticated read of repositories.", "Use SSH or HTTPS with auth."),
    10000: ("webmin", "Admin panel with a patch history worth watching.", "Patch, restrict, MFA."),
    10250: ("kubelet", "Node API can run commands in pods.", "Disable anonymous auth, webhook authz."),
    11211: ("memcached", "Large UDP amplification and data leaks.", "Disable UDP, bind to localhost, SASL."),
    11434: ("ollama", "Unauthenticated model API by default.", "Bind to localhost or Tailscale; put an auth proxy in front."),
    15672: ("rabbitmq-mgmt", "Management UI with the default guest account.", "Remove guest, restrict."),
    25565: ("minecraft", "Game server; keep it current.", "Keep updated, whitelist players."),
    27017: ("mongodb", "Open databases have been widely exposed.", "Enable auth, bind to app hosts."),
    32400: ("plex", "Media server; guard the account.", "Keep updated, strong account security."),
    41641: ("tailscale (udp)", "WireGuard-based mesh VPN; low risk.", "Use ACLs in the Tailscale admin console."),
    47808: ("bacnet (udp)", "Building automation without auth.", "Isolate OT networks."),
    49152: ("windows-rpc-dynamic", "Start of the dynamic RPC range.", "Firewall RPC from untrusted networks."),
    51820: ("wireguard (udp)", "VPN endpoint; silent to unauthenticated packets.", "Protect private keys, restrict AllowedIPs."),
}  # fmt: skip

TOP_PORTS = (
    21, 22, 23, 25, 53, 79, 80, 81, 88, 110, 111, 113, 119, 135, 139, 143, 179, 389, 427, 443,
    445, 464, 465, 515, 548, 554, 587, 593, 631, 636, 853, 873, 902, 990, 993, 995, 1080, 1194,
    1433, 1521, 1723, 1883, 2049, 2082, 2083, 2181, 2222, 2375, 2376, 2379, 3000, 3128, 3268,
    3306, 3389, 3690, 4369, 4443, 4786, 5000, 5001, 5060, 5061, 5432, 5601, 5672, 5900, 5985,
    5986, 6000, 6379, 6443, 6667, 7001, 8000, 8006, 8008, 8009, 8080, 8081, 8086, 8088, 8123,
    8291, 8443, 8765, 8888, 9000, 9042, 9090, 9100, 9200, 9418, 10000, 10250, 11211, 11434,
    15672, 27017, 32400,
)  # fmt: skip
MAX_SCAN_PORTS = 1024
SCAN_CONCURRENCY = 64

# Addresses ``scan_ports`` may reach without an inventory entry: RFC 1918, loopback,
# link-local, Tailscale's CGNAT range and their IPv6 counterparts.
SCANNABLE = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
        "100.64.0.0/10", "::1/128", "fe80::/10", "fc00::/7",
    )
)  # fmt: skip


def port_info(port: int) -> dict[str, Any]:
    if port in PORTS:
        service, risk, hardening = PORTS[port]
        return {"port": port, "service": service, "risk": risk, "hardening": hardening}
    if port < 1024:
        band = "well-known range (0-1023): normally a system service"
    elif port < 49152:
        band = "registered range (1024-49151): an application service"
    else:
        band = "dynamic range (49152-65535): usually a temporary client port or a high RPC port"
    return {
        "port": port,
        "service": "unknown",
        "risk": f"Not in the bundled table; {band}.",
        "hardening": "Find the process listening on it (ss -tlpn, netstat -abno) and close it "
        "if nothing needs it.",
    }


def parse_ports(spec: str) -> list[int]:
    """``top100``, ``all-known`` or a list like ``22,80,8000-8100``."""
    spec = spec.strip().lower().replace(" ", "")
    if spec in ("", "top100", "top", "common"):
        return list(TOP_PORTS)
    if spec in ("all-known", "known"):
        return sorted(p for p, (name, _, _) in PORTS.items() if "udp" not in name)
    ports: set[int] = set()
    for part in spec.split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", part)
        if not m:
            raise ToolError(f"Couldn't read the ports '{part}'. Use e.g. 22,80,8000-8100.")
        low, high = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= low <= high <= 65535:
            raise ToolError(f"'{part}' is not a valid port range.")
        ports.update(range(low, high + 1))
        if len(ports) > MAX_SCAN_PORTS:
            raise ToolError(f"Check at most {MAX_SCAN_PORTS} ports at a time.")
    if not ports:
        raise ToolError("No ports given.")
    return sorted(ports)


@tool(category="security", summary="Explain port {port}")
def explain_port(port: Annotated[int, "TCP or UDP port number"]) -> dict[str, Any]:
    """What usually runs on a port, what to watch for and how to harden it (from a bundled
    table of common services)."""
    if not 0 <= port <= 65535:
        raise ToolError("Ports go from 0 to 65535.")
    return port_info(port)


# Targets ----------------------------------------------------------------------------------------


def clean_target(host: str) -> str:
    """A bare host name or IP address from what the model sent (a URL is reduced to its host)."""
    host = host.strip()
    if "://" in host:
        host = urlsplit(host).hostname or ""
    host = host.strip("[]").rstrip(".")
    if not host:
        raise ToolError("Give a host name or IP address.")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not work.HOST.fullmatch(host):
        raise ToolError(f"'{host[:80]}' is not a valid host name.")
    return host.lower()


async def resolve(host: str) -> list[str]:
    """The addresses ``host`` resolves to (the address itself when it is already an IP)."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"Couldn't resolve {host}.") from exc
    seen: list[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%")[0]
        if ip not in seen:
            seen.append(ip)
    return seen


def is_scannable(addresses: list[str]) -> bool:
    """True when every address is private, loopback, link-local or in Tailscale's range."""
    if not addresses:
        return False
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if not any(ip in net for net in SCANNABLE):
            return False
    return True


async def _probe_port(host: str, port: int, timeout: float) -> bool:
    """Open and close a TCP connection; True when the port accepts it."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False
    writer.close()
    with contextlib.suppress(asyncio.TimeoutError, OSError):
        await asyncio.wait_for(writer.wait_closed(), 1.0)
    return True


@tool(
    category="security",
    risk="confirm",
    summary="Check open ports on {host}",
    timeout=120.0,
)
async def scan_ports(
    ctx: ToolContext,
    host: Annotated[str, "Host name or IP address on your own network"],
    ports: Annotated[str, "top100, all-known, or a list like 22,80,8000-8100"] = "top100",
    timeout_ms: Annotated[int, "Per-port connect timeout in milliseconds"] = 400,
) -> dict[str, Any]:
    """Check which TCP ports accept a connection on a host you own, with a note on each open
    port. Only hosts that resolve to a private, loopback, link-local or Tailscale address are
    allowed, unless the host is in the work asset inventory; public addresses are refused
    because you need the owner's authorisation to probe them. Asks you first."""
    target = clean_target(host)
    wanted = parse_ports(ports)
    addresses = await resolve(target)
    if not (is_scannable(addresses) or work.in_inventory(ctx.store, target, addresses)):
        where = ", ".join(addresses[:3]) or "a public address"
        raise ToolError(
            f"{target} resolves to {where}. I only check hosts on your own network (private, "
            "loopback, link-local or Tailscale addresses) or hosts in your asset inventory. "
            "Probing a public host without the owner's written authorisation isn't something "
            "I'll do."
        )
    address = addresses[0]
    timeout = min(max(timeout_ms, 50), 3000) / 1000
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(port: int) -> int | None:
        async with gate:
            return port if await _probe_port(address, port, timeout) else None

    started = time.perf_counter()
    results = await asyncio.gather(*(one(p) for p in wanted))
    open_ports = [p for p in results if p is not None]
    return {
        "host": target,
        "address": address,
        "scanned": len(wanted),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "open_ports": [port_info(p) for p in open_ports],
        "closed_or_filtered": len(wanted) - len(open_ports),
        "note": "A connect check: open means the port accepted a TCP connection. It does not "
        "confirm the service or that it is vulnerable.",
    }


@tool(category="security", summary="Check the TLS certificate of {host}")
async def tls_check(
    ctx: ToolContext,
    host: Annotated[str, "Host name to connect to"],
    port: Annotated[int, "TLS port"] = 443,
) -> dict[str, Any]:
    """Read a host's TLS certificate: subject, issuer, SANs, expiry and days left, protocol and
    cipher, and whether it matches the host name. Public hosts are fine; a private address is
    only allowed when it is in your asset inventory. Does not change anything."""
    target = clean_target(host)
    if not 1 <= port <= 65535:
        raise ToolError("Ports go from 1 to 65535.")
    addresses = await resolve(target)
    private = any(not ipaddress.ip_address(a).is_global for a in addresses if a)
    if private and not work.in_inventory(ctx.store, target, addresses):
        raise ToolError(
            f"{target} is on a private network and isn't in your asset inventory. Add it to the "
            "inventory first, so it's a host you've recorded as yours."
        )
    try:
        info = await work.tls_probe(target, port, address=addresses[0], timeout=8.0)
    except (asyncio.TimeoutError, ConnectionRefusedError) as exc:
        raise ToolError(f"Couldn't connect to {target}:{port}.") from exc
    except (ssl.SSLError, OSError, ValueError, IndexError) as exc:
        raise ToolError(f"TLS handshake with {target}:{port} failed: {exc}") from exc
    findings = []
    if info["expired"]:
        findings.append("certificate has expired")
    elif info["days_left"] < 21:
        findings.append(f"expires in {info['days_left']} days")
    if not info["hostname_match"]:
        findings.append(f"certificate does not cover {target}")
    if not info["trusted"]:
        findings.append(f"not trusted: {info['verify_error']}")
    return {
        "host": target,
        "port": port,
        "subject": info["subject"],
        "issuer": info["issuer"],
        "san": info["san"][:30],
        "not_before": info["not_before"],
        "not_after": info["not_after"],
        "days_left": info["days_left"],
        "expired": info["expired"],
        "protocol": info["protocol"],
        "cipher": info["cipher"],
        "hostname_match": info["hostname_match"],
        "trusted": info["trusted"],
        "findings": findings or ["certificate looks healthy"],
    }


# Decoding ---------------------------------------------------------------------------------------

MAX_DECODE_INPUT = 200_000
MAX_DECODE_OUTPUT = 8_000
ENCODINGS = ("auto", "base64", "base64url", "hex", "url", "html", "rot13", "gzip", "jwt",
             "quoted-printable")  # fmt: skip
_B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_HEX = re.compile(r"^(?:0x)?[0-9A-Fa-f]+$")
_JWT = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


def _as_text(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    return text if len(text) <= MAX_DECODE_OUTPUT else text[:MAX_DECODE_OUTPUT] + "…[truncated]"


def _printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return 0.0
    printable = sum(c.isprintable() or c in "\r\n\t" for c in text)
    return printable / len(text)


def _b64_raw(text: str, url: bool) -> bytes | None:
    """Decode base64 when the charset and length fit, without judging the output."""
    stripped = re.sub(r"\s", "", text)
    pattern = _B64URL if url else _B64
    if len(stripped) < 8 or len(stripped.rstrip("=")) % 4 == 1 or not pattern.match(stripped):
        return None
    body = stripped.rstrip("=")
    padded = (body + "=" * (-len(body) % 4)).encode()
    decoder = base64.urlsafe_b64decode if url else base64.b64decode
    try:
        return decoder(padded) or None
    except (binascii.Error, ValueError):
        return None


def _try_base64(text: str, url: bool) -> bytes | None:
    """Base64 that decodes to readable text (so auto-decode doesn't pick up random strings)."""
    data = _b64_raw(text, url)
    return data if data is not None and _printable_ratio(data) > 0.85 else None


def decode_jwt(token: str) -> dict[str, Any]:
    """Decode a JWT's header and payload without verifying the signature."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ToolError("A JWT has three dot-separated parts.")

    def segment(part: str) -> Any:
        raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        return json.loads(raw.decode("utf-8", "replace"))

    try:
        header, payload = segment(parts[0]), segment(parts[1])
    except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
        raise ToolError(f"Not a readable JWT: {exc}") from exc
    flags = []
    alg = str(header.get("alg", "")).lower()
    if alg in ("none", ""):
        flags.append("alg is 'none': the token is unsigned and must not be trusted")
    now = int(time.time())
    for claim, label in (("exp", "expired"), ("nbf", "not valid yet")):
        value = payload.get(claim)
        if isinstance(value, (int, float)):
            when = datetime.fromtimestamp(value, timezone.utc).isoformat()
            if claim == "exp" and value < now:
                flags.append(f"{label} (exp {when})")
            if claim == "nbf" and value > now:
                flags.append(f"{label} (nbf {when})")
    return {
        "header": header,
        "payload": payload,
        "signature_present": bool(parts[2]),
        "signature_verified": False,
        "flags": flags,
    }


def _try_gzip(data: bytes) -> bytes | None:
    for opener in (
        lambda d: zlib.decompress(d, 31),  # gzip
        lambda d: zlib.decompress(d),  # zlib
        lambda d: zlib.decompress(d, -15),  # raw deflate
    ):
        try:
            out = opener(data)
        except (zlib.error, OSError):
            continue
        if out:
            return out
    return None


def _decode_once(data: str, encoding: str) -> tuple[str, Any] | None:
    """Apply one decoding. Returns (label, result) or None when it doesn't apply."""
    if encoding == "jwt":
        return "jwt", decode_jwt(data)
    if encoding in ("gzip", "zlib"):
        try:
            raw = base64.b64decode(re.sub(r"\s", "", data), validate=False)
        except (binascii.Error, ValueError):
            raw = data.encode("latin-1", "ignore")
        out = _try_gzip(raw)
        if out is None:
            raise ToolError("Not gzip/zlib data (optionally base64-wrapped).")
        return "gzip/zlib", _as_text(out)
    if encoding in ("base64", "base64url"):
        out = _try_base64(data, encoding == "base64url")
        if out is None:
            raise ToolError("Not valid base64 (or it decodes to binary).")
        return encoding, _as_text(out)
    if encoding == "hex":
        cleaned = re.sub(r"(?:0x|\s|:)", "", data)
        if not cleaned or len(cleaned) % 2 or not _HEX.match(cleaned):
            raise ToolError("Not valid hexadecimal.")
        return "hex", _as_text(bytes.fromhex(cleaned))
    if encoding == "url":
        return "url", unquote(data)
    if encoding == "html":
        return "html", html.unescape(data)
    if encoding == "rot13":
        import codecs

        return "rot13", codecs.decode(data, "rot13")
    if encoding == "quoted-printable":
        return "quoted-printable", _as_text(quopri.decodestring(data.encode()))
    return None


def _auto_step(data: str) -> tuple[str, str] | None:
    """One layer of auto-decoding: the first thing that cleanly applies, most specific first."""
    stripped = data.strip()
    if _JWT.match(stripped):
        return None  # Handled separately so the structured result is kept.
    for url, label in ((False, "base64"), (True, "base64url")):
        raw = _b64_raw(stripped, url)
        if raw is None:
            continue
        gz = _try_gzip(raw)
        if gz is not None:
            return (f"{label}+gzip/zlib", _as_text(gz))
        if _printable_ratio(raw) > 0.85:
            return (label, _as_text(raw))
    cleaned = re.sub(r"(?:0x|\s|:)", "", stripped)
    if len(cleaned) >= 8 and len(cleaned) % 2 == 0 and _HEX.match(cleaned):
        decoded = bytes.fromhex(cleaned)
        if _printable_ratio(decoded) > 0.85:
            return ("hex", _as_text(decoded))
    if "%" in data and re.search(r"%[0-9A-Fa-f]{2}", data):
        unquoted = unquote(data)
        if unquoted != data:
            return ("url", unquoted)
    if re.search(r"&(?:#\d+|#x[0-9A-Fa-f]+|[a-zA-Z]+);", data):
        unescaped = html.unescape(data)
        if unescaped != data:
            return ("html", unescaped)
    if "=" in data and re.search(r"=[0-9A-Fa-f]{2}", data):
        qp = _as_text(quopri.decodestring(data.encode()))
        if qp != data and "=" not in re.sub(r"=[0-9A-Fa-f]{2}", "", data).strip()[-1:]:
            return ("quoted-printable", qp)
    return None


@tool(category="security", summary="Decode data ({encoding})")
def decode(
    data: Annotated[str, "The suspicious data to decode"],
    encoding: Annotated[
        str,
        "auto, base64, base64url, hex, url, html, rot13, gzip, jwt or quoted-printable",
    ] = "auto",
) -> dict[str, Any]:
    """Decode pasted data without ever running it. ``auto`` peels layered encodings (up to four
    levels, e.g. base64 of a gzip of JSON) and reports each step. Handles base64 / base64url,
    hex, URL and HTML-entity escaping, ROT13, gzip or zlib wrapped in base64, JWTs (header and
    payload only, never verified, flagging ``alg: none`` and expiry) and quoted-printable.
    Nothing is executed and the output is capped."""
    encoding = encoding.strip().lower().replace("_", "-") or "auto"
    if encoding in ("base64-url", "b64url"):
        encoding = "base64url"
    if encoding in ("b64", "base-64"):
        encoding = "base64"
    if encoding not in ENCODINGS:
        raise ToolError(f"Unknown encoding. Choose from: {', '.join(ENCODINGS)}.")
    if not data.strip():
        raise ToolError("There is nothing to decode.")
    if len(data) > MAX_DECODE_INPUT:
        raise ToolError(f"Give at most {MAX_DECODE_INPUT} characters.")

    if encoding != "auto":
        result = _decode_once(data, encoding)
        assert result is not None
        label, value = result
        return {"input_encoding": encoding, "steps": [label], "result": value}

    steps: list[dict[str, Any]] = []
    current = data
    for _ in range(4):
        if _JWT.match(current.strip()):
            steps.append({"encoding": "jwt", "result": decode_jwt(current)})
            break
        step = _auto_step(current)
        if step is None:
            break
        label, value = step
        steps.append({"encoding": label, "result": value})
        current = value
        if not isinstance(current, str):
            break
    if not steps:
        return {
            "input_encoding": "auto",
            "steps": [],
            "result": data[:MAX_DECODE_OUTPUT],
            "note": "No encoding recognised; this looks like plain text.",
        }
    return {
        "input_encoding": "auto",
        "layers": len(steps),
        "steps": [s["encoding"] for s in steps],
        "detail": steps,
        "result": steps[-1]["result"],
    }


# Hashes -----------------------------------------------------------------------------------------

# Prefixed (crypt-style) hashes: prefix -> (name, hashcat mode).
HASH_PREFIXES: tuple[tuple[str, str, int | None], ...] = (
    ("$2a$", "bcrypt", 3200),
    ("$2b$", "bcrypt", 3200),
    ("$2y$", "bcrypt", 3200),
    ("$2x$", "bcrypt", 3200),
    ("$argon2id$", "Argon2id", 34000),
    ("$argon2i$", "Argon2i", 34000),
    ("$argon2d$", "Argon2d", None),
    ("$y$", "yescrypt", None),
    ("$7$", "scrypt", 8900),
    ("$gy$", "gost-yescrypt", None),
    ("$6$", "sha512crypt", 1800),
    ("$5$", "sha256crypt", 7400),
    ("$1$", "md5crypt", 500),
    ("$apr1$", "Apache md5crypt (apr1)", 1600),
    ("$sha1$", "sha1crypt", 15100),
    ("$pbkdf2-sha256$", "PBKDF2-HMAC-SHA256", 10900),
    ("$pbkdf2-sha512$", "PBKDF2-HMAC-SHA512", 12100),
    ("{SSHA}", "LDAP SSHA (salted SHA-1)", 111),
    ("{SHA}", "LDAP SHA-1", 101),
    ("sha1$", "Django SHA-1", 124),
    ("pbkdf2_sha256$", "Django PBKDF2-SHA256", 10000),
    ("$DCC2$", "Domain Cached Credentials 2 (mscash2)", 2100),
    ("$ml$", "macOS PBKDF2-SHA512 (10.8+)", 7100),
    ("$NT$", "NTLM", 1000),
    ("0x0100", "MSSQL 2005 (SHA-1)", 132),
    ("0x0200", "MSSQL 2012/2014 (SHA-512)", 1731),
    ("$krb5tgs$", "Kerberos 5 TGS-REP", 13100),
    ("$krb5asrep$", "Kerberos 5 AS-REP", 18200),
)

# Plain hex hashes by length (lowercase length -> candidates, most likely first).
HEX_LENGTHS: dict[int, list[tuple[str, int | None]]] = {
    32: [
        ("MD5", 0),
        ("NTLM", 1000),
        ("MD4", 900),
        ("LM (half)", 3000),
        ("MD5(halved)", None),
    ],
    40: [("SHA-1", 100), ("RIPEMD-160", 6000), ("MySQL4.1+ SHA1(SHA1)", 300)],
    56: [("SHA-224", 1300), ("SHA3-224", 17300)],
    64: [
        ("SHA-256", 1400),
        ("SHA3-256", 17400),
        ("BLAKE2s-256", None),
        ("Keccak-256", 17800),
        ("GOST R 34.11-94", 6900),
    ],
    96: [("SHA-384", 10800), ("SHA3-384", 17500)],
    128: [("SHA-512", 1700), ("SHA3-512", 17600), ("BLAKE2b-512", 600), ("Whirlpool", 6100)],
    16: [("MySQL 3.2.3 (old)", 200), ("CRC-64", None)],
    8: [("CRC-32", None), ("Adler-32", None)],
}


@tool(category="security", summary="Identify hash type")
def identify_hash(value: Annotated[str, "The hash string"]) -> dict[str, Any]:
    """Guess which algorithm produced a hash, from its length, character set and any prefix
    (MD5, SHA-1/256/512, NTLM, bcrypt, sha512crypt, Argon2, yescrypt and more), with the
    matching hashcat mode numbers. Lengths overlap, so it returns ranked candidates."""
    raw = value.strip()
    if not raw:
        raise ToolError("Give a hash to identify.")
    candidates: list[dict[str, Any]] = []
    notes: list[str] = []

    def add(name: str, mode: int | None, note: str = "") -> None:
        candidates.append({"name": name, "hashcat_mode": mode, "note": note})

    lower = raw.lower()
    for prefix, name, mode in HASH_PREFIXES:
        if raw.startswith(prefix) or lower.startswith(prefix.lower()):
            add(name, mode)
    if raw.count(":") == 1 and all(re.fullmatch(r"[0-9A-Fa-f]{32}", p) for p in raw.split(":")):
        add("LM:NTLM pair (pwdump)", 1000, "left half LM, right half NTLM")
    if (m := re.fullmatch(r"([0-9A-Fa-f]{32}):(.+)", raw)) and len(m.group(2)) <= 64:
        add("md5(salt) or md5crypt variant", 10, "looks like hash:salt")
    if not candidates and re.fullmatch(r"[0-9A-Fa-f]+", raw):
        for name, mode in HEX_LENGTHS.get(len(raw), []):
            add(name, mode)
        if len(raw) not in HEX_LENGTHS:
            notes.append(f"{len(raw)} hex chars doesn't match a common fixed-length digest.")
    if not candidates and re.fullmatch(r"[A-Za-z0-9+/]{20,}={0,2}", raw) and len(raw) % 4 == 0:
        notes.append("Looks like base64; try the decode tool, then identify the result.")
    if not candidates and not notes:
        notes.append("Unrecognised. Check for a prefix like $6$ or a known fixed length.")
    return {
        "value": raw if len(raw) <= 120 else raw[:117] + "…",
        "length": len(raw),
        "candidates": candidates,
        "notes": notes,
        "hint": "Lengths and shapes overlap (MD5 and NTLM are both 32 hex), so the list is "
        "ranked guesses, not a certain identification.",
    }


# Log analysis -----------------------------------------------------------------------------------

MAX_LOG_BYTES = 2_000_000
LOG_KINDS = ("auto", "auth", "web", "generic")
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
IPV6 = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4}\b")
# auth / ssh style failures.
FAIL_AUTH = re.compile(
    r"(?i)(failed password|authentication failure|invalid user|failed login|"
    r"login failed|auth(?:entication)? failed|permission denied|fatal: .*authenticating|"
    r"did not receive identification|maximum authentication attempts|possible break-in)"
)
SUCCESS_AUTH = re.compile(r"(?i)(accepted password|accepted publickey|session opened|new session)")
USER_PAT = re.compile(
    r"(?i)(?:invalid user |user[= ]|for (?:invalid user )?|account[= ])([A-Za-z0-9._\-\\$]{1,64})"
)
SUDO_PAT = re.compile(r"(?i)sudo:.*COMMAND=(\S+)")
HTTP_LINE = re.compile(
    r'(?P<ip>\S+) \S+ \S+ \[[^\]]+\] "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" (?P<status>\d{3}) '
    r'(?P<size>\S+)(?: "(?P<ref>[^"]*)" "(?P<ua>[^"]*)")?'
)
# Commands and payloads worth a closer look in logs (not executed, just counted).
SUSPICIOUS = {
    "pipe to shell": re.compile(r"(?i)(?:curl|wget)\b[^|&;]*[|]\s*(?:ba|z|da|)sh\b"),
    "base64 decode": re.compile(r"(?i)base64\s+(?:--decode|-d|-D)\b"),
    "netcat exec": re.compile(r"(?i)\bnc\b[^\n]*\s-[a-z]*e\b|\bncat\b[^\n]*--exec"),
    "/dev/tcp redirect": re.compile(r"/dev/(?:tcp|udp)/"),
    "reverse shell": re.compile(
        r"(?i)(bash|sh)\s+-i\b.*(?:/dev/tcp|>&)|python[0-9.]*\s+-c.*socket"
    ),
    "powershell encoded": re.compile(r"(?i)powershell(?:\.exe)?\b[^\n]*-e(?:nc|ncodedcommand)?\b"),
    "download+run": re.compile(
        r"(?i)(?:iwr|invoke-webrequest|wget|curl)[^\n]*(?:iex|invoke-expression|;\s*\./)"
    ),
    "path traversal": re.compile(r"(?:\.\./){2,}|%2e%2e%2f"),
    "sql injection": re.compile(r"(?i)(?:union\s+select|or\s+1=1|sleep\(\d|' or ')"),
    "template/log4shell": re.compile(r"\$\{(?:jndi:|env:|::-)"),
}
# User agents that announce themselves as scanners or tools.
SCANNER_UA = re.compile(
    r"(?i)\b(sqlmap|nikto|nmap|masscan|zgrab|nuclei|dirbuster|gobuster|wpscan|acunetix|"
    r"nessus|openvas|hydra|metasploit|curl|wget|python-requests|go-http-client|libwww|"
    r"censys|shodan|zmap|httpx|feroxbuster|whatweb)\b"
)


def _top(counter: Counter[str], n: int = 10) -> list[dict[str, Any]]:
    return [{"value": value, "count": count} for value, count in counter.most_common(n)]


@tool(category="security", summary="Analyse log for signs of trouble")
def analyze_log(
    text: Annotated[str, "Log text to analyse"],
    kind: Annotated[str, "auth, web, generic or auto"] = "auto",
) -> dict[str, Any]:
    """Scan pasted log text for the things worth a human's attention: source IPs, user names,
    failed-authentication bursts, HTTP status-code spikes, scanner user agents and command
    patterns often seen in attacks (curl|sh, base64 -d, nc -e, /dev/tcp and similar). Returns
    counts and the top entries for you to interpret; it reads the log, it never runs anything
    in it."""
    kind = kind.strip().lower() or "auto"
    if kind not in LOG_KINDS:
        raise ToolError(f"kind must be one of: {', '.join(LOG_KINDS)}.")
    if not text.strip():
        raise ToolError("There is no log text to analyse.")
    if len(text.encode("utf-8", "ignore")) > MAX_LOG_BYTES:
        raise ToolError(f"Give at most {MAX_LOG_BYTES // 1_000_000} MB of log text.")
    lines = text.splitlines()
    if kind == "auto":
        http_hits = sum(1 for ln in lines[:500] if HTTP_LINE.search(ln))
        kind = "web" if http_hits >= max(3, len(lines[:500]) // 10) else "auth"

    ips: Counter[str] = Counter()
    users: Counter[str] = Counter()
    failed_by_ip: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    paths: Counter[str] = Counter()
    user_agents: Counter[str] = Counter()
    scanners: Counter[str] = Counter()
    sudo: Counter[str] = Counter()
    suspicious: dict[str, list[str]] = {}
    fails = successes = 0

    for line in lines:
        for ip in IPV4.findall(line) or IPV6.findall(line):
            ips[ip] += 1
        if FAIL_AUTH.search(line):
            fails += 1
            for ip in IPV4.findall(line):
                failed_by_ip[ip] += 1
            if m := USER_PAT.search(line):
                users[m.group(1)] += 1
        if SUCCESS_AUTH.search(line):
            successes += 1
        if m := SUDO_PAT.search(line):
            sudo[m.group(1)] += 1
        if m := HTTP_LINE.search(line):
            statuses[m.group("status")] += 1
            paths[m.group("path")[:120]] += 1
            ua = m.group("ua") or ""
            if ua and ua != "-":
                user_agents[ua[:120]] += 1
            if sm := SCANNER_UA.search(ua):
                scanners[sm.group(1).lower()] += 1
        for label, pattern in SUSPICIOUS.items():
            if pattern.search(line):
                suspicious.setdefault(label, [])
                if len(suspicious[label]) < 5:
                    suspicious[label].append(line.strip()[:200])

    status_classes = Counter(f"{s[0]}xx" for s in statuses)
    result: dict[str, Any] = {
        "kind": kind,
        "lines": len(lines),
        "unique_ips": len(ips),
        "top_ips": _top(ips),
        "failed_auth": {
            "total": fails,
            "successful_auth": successes,
            "top_sources": _top(failed_by_ip),
            "targeted_users": _top(users),
        },
        "sudo_commands": _top(sudo),
        "suspicious": [
            {"pattern": label, "count": len(samples), "examples": samples}
            for label, samples in suspicious.items()
        ],
    }
    if kind == "web":
        result["http"] = {
            "requests": sum(statuses.values()),
            "status_classes": dict(status_classes),
            "status_codes": _top(statuses),
            "top_paths": _top(paths),
            "scanner_user_agents": _top(scanners),
            "top_user_agents": _top(user_agents, 8),
        }
    brute = [row for row in result["failed_auth"]["top_sources"] if row["count"] >= 10]
    hints = []
    if brute:
        hints.append(
            f"{brute[0]['value']} has {brute[0]['count']} failed logins: looks like password "
            "guessing."
        )
    if scanners:
        hints.append("Scanner/tool user agents are present: likely automated probing.")
    if suspicious:
        hints.append("Command patterns worth reading in full: " + ", ".join(suspicious) + ".")
    if status_classes.get("5xx", 0) > status_classes.get("2xx", 0) and status_classes.get("5xx"):
        hints.append("More 5xx than 2xx responses: the server may be failing or overloaded.")
    result["hints"] = hints
    return result


# CVE lookup -------------------------------------------------------------------------------------

CVE_ID = re.compile(r"(?i)\bCVE-(\d{4})-(\d{4,})\b")
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
CIRCL_URL = "https://cve.circl.lu/api/cve/"
CVE_CACHE_TTL = 24 * 3600
MAX_CPES = 12
MAX_REFERENCES = 8
CVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cve_cache (
    cve_id     TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""
_cve_ready: weakref.WeakSet[Any] = weakref.WeakSet()


def _cve_store(ctx: ToolContext) -> Any:
    if ctx.store not in _cve_ready:
        ctx.store.ensure_schema(CVE_SCHEMA)
        _cve_ready.add(ctx.store)
    return ctx.store


def normalise_cve(value: str) -> str:
    m = CVE_ID.search(value or "")
    if not m:
        raise ToolError("Give a CVE id like CVE-2024-3094.")
    return f"CVE-{m.group(1)}-{m.group(2)}"


def _english(descriptions: list[dict[str, Any]]) -> str:
    for desc in descriptions:
        if desc.get("lang") == "en":
            return str(desc.get("value", "")).strip()
    return str(descriptions[0].get("value", "")).strip() if descriptions else ""


def _nvd_metric(metrics: dict[str, Any]) -> dict[str, Any]:
    """The best available CVSS score from an NVD metrics block (v4, then v3.1, v3.0, v2)."""
    for key, version in (
        ("cvssMetricV40", "4.0"),
        ("cvssMetricV31", "3.1"),
        ("cvssMetricV30", "3.0"),
        ("cvssMetricV2", "2.0"),
    ):
        entries = metrics.get(key)
        if not entries:
            continue
        primary = next((e for e in entries if e.get("type") == "Primary"), entries[0])
        data = primary.get("cvssData", {})
        return {
            "version": version,
            "base_score": data.get("baseScore"),
            "severity": data.get("baseSeverity") or primary.get("baseSeverity"),
            "vector": data.get("vectorString"),
        }
    return {}


def parse_nvd(payload: dict[str, Any], cve_id: str) -> dict[str, Any] | None:
    vulns = payload.get("vulnerabilities") or []
    cve = next(
        (v["cve"] for v in vulns if v.get("cve", {}).get("id", "").upper() == cve_id.upper()),
        None,
    )
    if cve is None:
        return None
    cpes: list[str] = []
    for config in cve.get("configurations", []):
        for node in config.get("nodes", []):
            for match in node.get("cpeMatch", []):
                if match.get("vulnerable") and match.get("criteria"):
                    cpes.append(match["criteria"])
    references = [r.get("url") for r in cve.get("references", []) if r.get("url")]
    cwes = []
    for weakness in cve.get("weaknesses", []):
        for desc in weakness.get("description", []):
            if desc.get("value", "").startswith("CWE-") and desc["value"] not in cwes:
                cwes.append(desc["value"])
    kev = bool(cve.get("cisaExploitAdd")) or cve.get("cisaVulnerabilityName") is not None
    metric = _nvd_metric(cve.get("metrics", {}))
    return {
        "id": cve["id"],
        "source": "NVD",
        "published": cve.get("published"),
        "modified": cve.get("lastModified"),
        "status": cve.get("vulnStatus"),
        "description": _english(cve.get("descriptions", [])),
        "cvss": metric,
        "cwe": cwes,
        "cisa_kev": kev,
        "kev_due": cve.get("cisaActionDue"),
        "affected": _summarise_cpes(cpes),
        "references": references[:MAX_REFERENCES],
    }


def _summarise_cpes(cpes: list[str]) -> dict[str, Any]:
    """A short view of the vulnerable CPE list: the vendor/product pairs, capped."""
    products: list[str] = []
    for cpe in cpes:
        parts = cpe.split(":")
        if len(parts) >= 5:
            label = f"{parts[3]} {parts[4]}".replace("_", " ")
            if label not in products:
                products.append(label)
    return {
        "product_count": len(products),
        "products": products[:MAX_CPES],
        "match_count": len(cpes),
        "truncated": len(products) > MAX_CPES,
    }


def parse_circl(data: dict[str, Any], cve_id: str) -> dict[str, Any] | None:
    """CIRCL returns NVD-shaped data under ``containers`` (CVE 5.x) or older flat fields."""
    if not data:
        return None
    container = (data.get("containers") or {}).get("cna") or {}
    meta = data.get("cveMetadata") or {}
    if container or meta:
        metrics = container.get("metrics") or []
        cvss: dict[str, Any] = {}
        for entry in metrics:
            for ver_key, version in (("cvssV4_0", "4.0"), ("cvssV3_1", "3.1"), ("cvssV3_0", "3.0")):
                block = entry.get(ver_key)
                if block and not cvss:
                    cvss = {
                        "version": version,
                        "base_score": block.get("baseScore"),
                        "severity": block.get("baseSeverity"),
                        "vector": block.get("vectorString"),
                    }
        descriptions = container.get("descriptions") or []
        cwes = []
        for pt in container.get("problemTypes", []):
            for desc in pt.get("descriptions", []):
                cid = desc.get("cweId") or ""
                if cid.startswith("CWE-") and cid not in cwes:
                    cwes.append(cid)
        refs = [r.get("url") for r in container.get("references", []) if r.get("url")]
        return {
            "id": meta.get("cveId") or cve_id,
            "source": "CIRCL",
            "published": meta.get("datePublished"),
            "modified": meta.get("dateUpdated"),
            "description": _english(descriptions),
            "cvss": cvss,
            "cwe": cwes,
            "cisa_kev": None,
            "affected": {
                "products": [
                    f"{a.get('vendor', '?')} {a.get('product', '?')}"
                    for a in container.get("affected", [])[:MAX_CPES]
                ]
            },
            "references": refs[:MAX_REFERENCES],
        }
    # Older CIRCL flat shape.
    if "summary" not in data and "id" not in data:
        return None
    return {
        "id": data.get("id") or cve_id,
        "source": "CIRCL",
        "published": data.get("Published"),
        "modified": data.get("Modified"),
        "description": data.get("summary", ""),
        "cvss": {
            "version": "3.x",
            "base_score": data.get("cvss"),
            "vector": data.get("cvss-vector"),
        },
        "cwe": [data["cwe"]] if data.get("cwe", "").startswith("CWE-") else [],
        "cisa_kev": None,
        "affected": {"products": (data.get("vulnerable_product") or [])[:MAX_CPES]},
        "references": (data.get("references") or [])[:MAX_REFERENCES],
    }


async def _fetch_cve(ctx: ToolContext, cve_id: str) -> dict[str, Any]:
    errors = []
    try:
        resp = await ctx.http.get(
            NVD_URL,
            params={"cveId": cve_id},
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
        )
        if resp.status_code == 200:
            parsed = parse_nvd(resp.json(), cve_id)
            if parsed:
                return parsed
            errors.append("NVD has no record for it")
        elif resp.status_code == 404:
            errors.append("NVD returned 404")
        else:
            errors.append(f"NVD returned HTTP {resp.status_code}")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        errors.append(f"NVD unreachable ({type(exc).__name__})")
    try:
        resp = await ctx.http.get(
            CIRCL_URL + cve_id, headers={"User-Agent": USER_AGENT}, timeout=15.0
        )
        if resp.status_code == 200 and resp.content:
            parsed = parse_circl(resp.json(), cve_id)
            if parsed:
                parsed["note"] = "From the CIRCL CVE service (NVD did not answer)."
                return parsed
        errors.append(f"CIRCL returned HTTP {resp.status_code}")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        errors.append(f"CIRCL unreachable ({type(exc).__name__})")
    raise ToolError(f"Couldn't look up {cve_id}: {'; '.join(errors)}.")


@tool(category="security", summary="Look up {cve_id}", timeout=40.0)
async def cve_lookup(
    ctx: ToolContext,
    cve_id: Annotated[str, "A CVE identifier, e.g. CVE-2024-3094"],
) -> dict[str, Any]:
    """Look up a CVE: its dates, description, CVSS base score, severity and vector, CWE ids, a
    summary of the affected products, references and whether CISA lists it as known-exploited.
    Queries the NVD API, falling back to the CIRCL service, and caches the answer for a day.
    Says how current the information is."""
    cve_id = normalise_cve(cve_id)
    store = _cve_store(ctx)
    cached = store.query_one("SELECT data, fetched_at FROM cve_cache WHERE cve_id = ?", (cve_id,))
    now = time.time()
    if cached and now - cached["fetched_at"] < CVE_CACHE_TTL:
        data = json.loads(cached["data"])
        data["cached"] = True
        data["retrieved"] = datetime.fromtimestamp(cached["fetched_at"]).isoformat(
            timespec="seconds"
        )
        return data
    data = await _fetch_cve(ctx, cve_id)
    data["cached"] = False
    data["retrieved"] = datetime.fromtimestamp(now).isoformat(timespec="seconds")
    store.execute(
        "INSERT INTO cve_cache (cve_id, data, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(cve_id) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
        (cve_id, json.dumps(data), now),
    )
    return data


# Unattended-run policy: cve_lookup brings text from the internet into the context; the pure
# tools only read what the user pasted.
brings_web("cve_lookup")
reads_private("scan_ports", "tls_check")
