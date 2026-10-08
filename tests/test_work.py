from __future__ import annotations

import time
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from bagley import work
from bagley.server import create_app
from bagley.store import Store
from tests.mock_llm import Reply

pytestmark = pytest.mark.anyio


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


# Inventory CRUD ---------------------------------------------------------------------------------


def test_add_and_get_asset_sets_defaults(store):
    asset = work.add_asset(
        store, {"client": "Acme", "name": "srv01", "kind": "server", "ip": "192.168.1.10"}
    )
    assert asset["id"] == 1
    assert asset["kind"] == "server"
    assert asset["checks"] == [{"type": "ping"}, {"type": "tcp", "port": 22}]
    assert asset["custom_checks"] is False
    fetched = work.get_asset(store, asset["id"])
    assert fetched["name"] == "srv01" and fetched["ip"] == "192.168.1.10"


def test_add_asset_normalises_kind_and_dates_and_tags():
    s = Store(":memory:")
    asset = work.add_asset(
        s,
        {
            "client": "Beta",
            "name": "lappy",
            "kind": "MacBook",
            "warranty_until": "31/12/2027",
            "tags": "vip, finance , vip",
        },
    )
    assert asset["kind"] == "laptop"
    assert asset["warranty_until"] == "2027-12-31"
    assert asset["tags"] == ["vip", "finance"]
    assert asset["checks"] == []  # Laptops aren't health-checked by default.
    s.close()


def test_add_asset_validation(store):
    with pytest.raises(work.AssetError, match="needs a client"):
        work.add_asset(store, {"name": "x"})
    with pytest.raises(work.AssetError, match="not a valid IP"):
        work.add_asset(store, {"client": "A", "name": "x", "ip": "999.1.1.1"})
    work.add_asset(store, {"client": "A", "name": "dup"})
    with pytest.raises(work.AssetError, match="already has an asset"):
        work.add_asset(store, {"client": "A", "name": "dup"})


def test_update_and_remove_asset(store):
    asset = work.add_asset(store, {"client": "Acme", "name": "srv01", "kind": "server"})
    updated = work.update_asset(store, asset["id"], {"ip": "10.0.0.9", "os": "Debian 12"})
    assert updated["ip"] == "10.0.0.9" and updated["os"] == "Debian 12"
    assert work.update_asset(store, 999, {"os": "x"}) is None
    assert work.remove_asset(store, asset["id"]) is True
    assert work.remove_asset(store, asset["id"]) is False
    assert work.get_asset(store, asset["id"]) is None


def test_custom_checks_round_trip(store):
    asset = work.add_asset(
        store,
        {
            "client": "Acme",
            "name": "web",
            "kind": "server",
            "checks": [{"type": "http", "target": "example.com", "expect_status": 200}],
        },
    )
    assert asset["custom_checks"] is True
    assert asset["checks"] == [{"type": "http", "target": "example.com", "expect_status": 200}]
    with pytest.raises(work.AssetError, match="tcp check needs a port"):
        work.add_asset(store, {"client": "A", "name": "b", "checks": [{"type": "tcp"}]})


def test_list_and_search(store):
    work.add_asset(store, {"client": "Acme", "name": "srv01", "ip": "10.0.0.1", "tags": "prod"})
    work.add_asset(store, {"client": "Acme", "name": "srv02", "ip": "10.0.0.2"})
    work.add_asset(store, {"client": "Beta", "name": "nas1", "hostname": "nas.beta.lan"})
    assert len(work.list_assets(store)) == 3
    assert len(work.list_assets(store, "Acme")) == 2
    assert [a["name"] for a in work.list_assets(store, query="nas")] == ["nas1"]
    assert [a["name"] for a in work.list_assets(store, query="prod")] == ["srv01"]
    assert [a["name"] for a in work.list_assets(store, query="10.0.0.2")] == ["srv02"]


def test_clients_summary(store):
    work.add_asset(store, {"client": "Acme", "name": "a"})
    work.add_asset(store, {"client": "Acme", "name": "b"})
    work.add_asset(store, {"client": "Beta", "name": "c"})
    names = {c["client"]: c["count"] for c in work.clients(store)}
    assert names == {"Acme": 2, "Beta": 1}


def test_in_inventory(store):
    work.add_asset(
        store, {"client": "A", "name": "srv", "hostname": "srv.lan", "ip": "203.0.113.5"}
    )
    assert work.in_inventory(store, "srv.lan")
    assert work.in_inventory(store, "203.0.113.5")
    assert work.in_inventory(store, "other", ["203.0.113.5"])
    assert not work.in_inventory(store, "unknown.example")


# CSV ---------------------------------------------------------------------------------------------


def test_import_csv_tolerant_headers(store):
    csv = (
        "Customer,Device Name,Type,IP Address,S/N,Warranty End,Assigned To\n"
        "Acme Corp,Reception PC,Desktop,192.168.1.20,ABC-123,2027-03-01,Jane Doe\n"
        "Acme Corp,Front Printer,MFP,192.168.1.30,PRN-9,31/01/2027,\n"
    )
    result = work.import_csv(store, csv)
    assert result["added"] == 2 and result["updated"] == 0
    assets = {a["name"]: a for a in work.list_assets(store)}
    assert assets["Reception PC"]["kind"] == "desktop"
    assert assets["Reception PC"]["serial"] == "ABC-123"
    assert assets["Reception PC"]["owner"] == "Jane Doe"
    assert assets["Front Printer"]["kind"] == "printer"
    assert assets["Front Printer"]["warranty_until"] == "2027-01-31"


def test_import_csv_semicolons_and_update(store):
    work.add_asset(store, {"client": "Acme", "name": "srv01", "ip": "10.0.0.1"})
    csv = "client;name;ip;os\nAcme;srv01;10.0.0.5;Ubuntu\nAcme;srv02;10.0.0.6;Ubuntu\n"
    result = work.import_csv(store, csv)
    assert result["added"] == 1 and result["updated"] == 1
    assert work.get_asset(store, 1)["ip"] == "10.0.0.5"


def test_import_csv_reports_errors_and_ignored_columns(store):
    csv = "name,ip,colour\ngood,10.0.0.1,blue\nbad,notanip,red\n"
    result = work.import_csv(store, csv, client="Acme")
    assert result["added"] == 1 and result["skipped"] == 1
    assert result["errors"][0]["line"] == 3
    assert "colour" in result["ignored_columns"]


def test_import_csv_needs_a_name_column(store):
    with pytest.raises(work.AssetError, match="name"):
        work.import_csv(store, "client,colour\nAcme,blue\n")


def test_export_round_trips(store):
    work.add_asset(
        store,
        {
            "client": "Acme",
            "name": "srv01",
            "kind": "server",
            "ip": "10.0.0.1",
            "tags": "prod, db",
            "checks": [{"type": "http", "target": "example.com"}],
        },
    )
    csv = work.export_csv(store)
    other = Store(":memory:")
    result = work.import_csv(other, csv)
    assert result["added"] == 1
    asset = work.list_assets(other)[0]
    assert asset["tags"] == ["prod", "db"]
    assert asset["checks"] == [{"type": "http", "target": "example.com"}]
    other.close()


def test_export_guards_formula_injection(store):
    work.add_asset(store, {"client": "Acme", "name": "=cmd()", "notes": "+danger"})
    csv = work.export_csv(store)
    assert "'=cmd()" in csv and "'+danger" in csv


# Health checks ----------------------------------------------------------------------------------


def make_runner(up: set[str]):
    """A fake ping runner: hosts in ``up`` reply, the rest do not."""

    async def runner(argv: list[str], timeout: float) -> tuple[int, str]:
        host = argv[-1]
        return (0, "time=1.20 ms") if host in up else (1, "")

    return runner


def http_transport(status: dict[str, int]):
    def handler(request: httpx.Request) -> httpx.Response:
        code = status.get(request.url.host, 200)
        return httpx.Response(code, text="ok")

    return httpx.MockTransport(handler)


def set_http(rt, transport: httpx.MockTransport) -> None:
    rt.http = httpx.AsyncClient(transport=transport, timeout=20.0)


async def test_check_assets_ping_and_http(make_runtime):
    rt = make_runtime()
    set_http(rt, http_transport({"down.example": 500}))
    work.add_asset(rt.store, {"client": "Acme", "name": "up", "kind": "server",
                              "ip": "192.168.1.10", "checks": [{"type": "ping"}]})  # fmt: skip
    work.add_asset(rt.store, {"client": "Acme", "name": "gone", "kind": "server",
                              "ip": "192.168.1.11", "checks": [{"type": "ping"}]})  # fmt: skip
    work.add_asset(rt.store, {"client": "Acme", "name": "web", "kind": "other",
                              "checks": [{"type": "http", "target": "ok.example"}]})  # fmt: skip
    work.add_asset(rt.store, {"client": "Acme", "name": "web5xx", "kind": "other",
                              "checks": [{"type": "http", "target": "down.example"}]})  # fmt: skip

    result = await work.check_assets(rt, "Acme", runner=make_runner({"192.168.1.10"}))
    by_name = {a["name"]: a for a in result["assets"]}
    assert by_name["up"]["status"] == "OK"
    assert by_name["gone"]["status"] == "CRIT"
    assert by_name["web"]["status"] == "OK"
    assert by_name["web5xx"]["status"] == "CRIT"
    assert result["summary"]["OK"] == 2 and result["summary"]["CRIT"] == 2
    assert "CLIENT ACME" in result["report"]


async def test_health_tls_days_left(make_runtime, monkeypatch):
    rt = make_runtime()

    async def fake_probe(host, port, *, address=None, timeout=5.0):
        return {
            "subject": {"CN": host}, "issuer": {}, "san": [host],
            "not_before": "2026-01-01T00:00:00+00:00", "not_after": "2099-01-01T00:00:00+00:00",
            "days_left": 5, "expired": False, "protocol": "TLSv1.3", "cipher": None,
            "hostname_match": True, "trusted": True, "verify_error": "",
        }  # fmt: skip

    monkeypatch.setattr(work, "tls_probe", fake_probe)
    work.add_asset(rt.store, {"client": "Acme", "name": "web", "kind": "other",
                              "checks": [{"type": "tls", "target": "example.com", "warn_days": 21}]})  # fmt: skip
    result = await work.check_assets(rt, "Acme")
    assert result["assets"][0]["status"] == "WARN"
    assert "5 days left" in result["assets"][0]["summary"]


async def test_status_change_detection(make_runtime):
    rt = make_runtime()
    work.add_asset(rt.store, {"client": "Acme", "name": "srv", "kind": "server",
                              "ip": "192.168.1.10", "checks": [{"type": "ping"}]})  # fmt: skip
    first = await work.check_assets(rt, runner=make_runner({"192.168.1.10"}))
    assert first["changes"] == []  # First run: nothing to compare against.
    assert first["assets"][0]["status"] == "OK"

    second = await work.check_assets(rt, runner=make_runner(set()))  # Now down.
    assert len(second["changes"]) == 1
    change = second["changes"][0]
    assert change["from"] == "OK" and change["to"] == "CRIT" and change["name"] == "srv"

    third = await work.check_assets(rt, runner=make_runner(set()))  # Still down.
    assert third["changes"] == []  # No change since last run.

    last = work.last_results(rt.store, "Acme")
    assert last["assets"][0]["status"] == "CRIT"


def test_warranty_finding():
    asset = {"warranty_until": "2026-12-01"}
    soon = work.warranty_finding(asset, date(2026, 10, 15))
    assert soon["status"] == "WARN" and soon["days"] == 47
    none = work.warranty_finding({"warranty_until": "2027-12-01"}, date(2026, 10, 15))
    assert none is None
    ended = work.warranty_finding({"warranty_until": "2026-01-01"}, date(2026, 10, 15))
    assert ended["status"] == "INFO"


# The health automation kind ---------------------------------------------------------------------


async def test_health_automation_kind_reports_and_notifies(make_runtime):
    rt = make_runtime()
    set_http(rt, http_transport({"gone.example": 500}))
    notes: list[dict[str, Any]] = []

    async def listener(event):
        notes.append(event)

    rt.listeners.add(listener)
    work.add_asset(rt.store, {"client": "Acme", "name": "web", "kind": "other",
                              "checks": [{"type": "http", "target": "gone.example"}]})  # fmt: skip

    item = rt.scheduler.create("health", "Acme health", "every 15 minutes", target="Acme")
    assert rt.store.get_automation(item["id"])["kind"] == "health"
    rt.store.update_automation(item["id"], next_run=time.time() - 1)
    await rt.scheduler.tick()

    done = rt.store.get_automation(item["id"])
    assert done["last_status"] == "crit"
    messages = rt.store.list_messages(done["conversation_id"])
    assert "HEALTH //" in messages[-1]["content"] and "CLIENT ACME" in messages[-1]["content"]
    note = next(e for e in notes if e["type"] == "notification")
    assert note["level"] == "critical" and "web" in note["body"]


async def test_health_automation_no_notification_without_new_problem(make_runtime):
    rt = make_runtime()
    set_http(rt, http_transport({}))  # Everything returns 200.
    work.add_asset(rt.store, {"client": "Acme", "name": "web", "kind": "other",
                              "checks": [{"type": "http", "target": "ok.example"}]})  # fmt: skip
    item = rt.scheduler.create("health", "All health", "every 15 minutes")
    notes: list[dict[str, Any]] = []

    async def listener(event):
        notes.append(event)

    rt.listeners.add(listener)
    rt.store.update_automation(item["id"], next_run=time.time() - 1)
    await rt.scheduler.tick()
    assert rt.store.get_automation(item["id"])["last_status"] == "ok"
    assert not [n for n in notes if n.get("type") == "notification"]


def test_health_kind_min_interval():
    from bagley.automations import EXTRA_KINDS, MIN_INTERVAL

    assert "health" in EXTRA_KINDS
    assert MIN_INTERVAL["health"] == 15 * 60


# Ticket summaries -------------------------------------------------------------------------------


def test_redact_and_languages():
    assert "[redacted]" in work.redact("the password: Hunter2!")
    assert "Hunter2" not in work.redact("password=Hunter2")
    assert work.clean_languages(["english", "FR"]) == ["English", "French"]
    assert work.clean_languages(None) == []
    with pytest.raises(ValueError):
        work.clean_languages(["English", "French", "German", "Spanish", "Italian"])


def test_parse_summary():
    text = (
        "TITLE: Printer back online\n"
        "SUMMARY: The reception printer was offline. We restarted it.\n"
        "DONE:\n- Power-cycled the printer\n- Checked the cable\n"
        "RESULT: It prints again.\n"
        "NEXT:\n- none\n"
    )
    parsed = work.parse_summary(text)
    assert parsed["title"] == "Printer back online"
    assert parsed["done"] == ["Power-cycled the printer", "Checked the cable"]
    assert parsed["next"] == []  # "none" dropped.
    assert work.parse_summary("no keys here at all") is None


ENGLISH = Reply(
    text=(
        "TITLE: Reception printer restored\n"
        "SUMMARY: The reception printer was offline this morning. We restarted it and "
        "confirmed it prints again.\n"
        "DONE:\n- Restarted the printer\n- Replaced the network cable\n"
        "RESULT: The printer is back online and working.\n"
        "NEXT:\n- Let us know if it drops offline again\n"
    )
)
FRENCH = Reply(
    text=(
        "TITLE: Imprimante de la réception rétablie\n"
        "SUMMARY: L'imprimante de la réception était hors ligne ce matin. Nous l'avons "
        "redémarrée et vérifié qu'elle imprime de nouveau.\n"
        "DONE:\n- Redémarrage de l'imprimante\n- Remplacement du câble réseau\n"
        "RESULT: L'imprimante est de nouveau en ligne.\n"
        "NEXT:\n- Prévenez-nous si elle se déconnecte à nouveau\n"
    )
)


async def test_ticket_summary_bilingual(make_runtime, mock):
    rt = make_runtime()
    mock.script = [ENGLISH, FRENCH]
    notes = (
        "Ticket #4821 P1. Reception HP printer offline. admin password: Hunter2. "
        "Restarted it on host prn01.internal, swapped cable. Works now. [internal note: tell L2]"
    )
    result = await work.ticket_summary(rt, notes, "Acme Corp")
    assert result["languages"] == ["English", "French"]
    assert "---" in result["markdown"]
    assert "**Summary**" in result["markdown"] and "**Résumé**" in result["markdown"]
    assert "Restarted the printer" in result["markdown"]
    assert "Redémarrage" in result["markdown"]
    assert result["machine"]

    # The notes are sent with secrets masked and flagged as untrusted.
    first = mock.requests[0]["messages"]
    system, user = first[0]["content"], first[1]["content"]
    assert "untrusted" in system.lower()
    assert "Hunter2" not in user and "[redacted]" in user
    assert "Client: Acme Corp" in user


async def test_ticket_summary_needs_notes(make_runtime):
    rt = make_runtime()
    with pytest.raises(ValueError, match="no notes"):
        await work.ticket_summary(rt, "   ")


# HTTP API ---------------------------------------------------------------------------------------


@pytest.fixture
def client(make_runtime, mock):
    rt = make_runtime()
    with TestClient(create_app(rt), base_url="http://localhost") as c:
        c.runtime = rt
        c.mock = mock
        yield c


def test_api_asset_crud(client):
    created = client.post(
        "/api/assets", json={"client": "Acme", "name": "srv01", "kind": "server", "ip": "10.0.0.1"}
    )
    assert created.status_code == 201
    asset_id = created.json()["id"]

    listing = client.get("/api/assets", params={"client": "Acme"}).json()
    assert listing["count"] == 1 and listing["assets"][0]["name"] == "srv01"

    patched = client.patch(f"/api/assets/{asset_id}", json={"fields": {"os": "Debian 12"}})
    assert patched.status_code == 200 and patched.json()["os"] == "Debian 12"

    clients = client.get("/api/assets/clients").json()["clients"]
    assert clients[0]["client"] == "Acme" and clients[0]["count"] == 1

    assert client.delete(f"/api/assets/{asset_id}").status_code == 204
    assert client.get("/api/assets").json()["count"] == 0
    assert client.patch(f"/api/assets/{asset_id}", json={"fields": {"os": "x"}}).status_code == 404


def test_api_asset_validation_error(client):
    resp = client.post("/api/assets", json={"client": "A", "name": "x", "ip": "bad"})
    assert resp.status_code == 422 and "IP" in resp.json()["detail"]


def test_api_csv_import_export(client):
    csv = "Customer,Device Name,IP Address\nAcme,pc1,10.0.0.5\nAcme,pc2,10.0.0.6\n"
    result = client.post("/api/assets/import", content=csv.encode()).json()
    assert result["added"] == 2
    export = client.get("/api/assets/export")
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    assert "pc1" in export.text and "pc2" in export.text


def test_api_health(client):
    client.post(
        "/api/assets",
        json={
            "client": "Acme",
            "name": "web",
            "kind": "other",
            "checks": [{"type": "http", "target": "example.com"}],
        },
    )
    result = client.post("/api/work/health", json={"client": "Acme"}).json()
    assert result["summary"]["OK"] == 1
    assert result["assets"][0]["status"] == "OK"
    last = client.get("/api/work/health/last", params={"client": "Acme"}).json()
    assert last["assets"][0]["status"] == "OK"


def test_api_ticket_summary(client):
    client.mock.script = [ENGLISH, FRENCH]
    resp = client.post(
        "/api/work/ticket-summary",
        json={"notes": "Printer offline, restarted it, works now.", "client": "Acme"},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["languages"] == ["English", "French"]
    assert "**Summary**" in body["markdown"] and "**Résumé**" in body["markdown"]


def test_api_ticket_summary_empty_notes(client):
    assert client.post("/api/work/ticket-summary", json={"notes": "   "}).status_code == 422
