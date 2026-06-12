from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.errors import ConflictError
from app.main import create_app
from app.services import dns
from tests.conftest import setup_and_login
from tests.test_cloudflare import FakeCF


def _soa(zone: str) -> dict:
    return {
        "name": zone,
        "type": "SOA",
        "ttl": 3600,
        "records": [{"content": f"ns1.{zone} hostmaster.{zone} 1 10800 3600 604800 3600"}],
    }


class FakePDNS:
    """In-memory PowerDNS double implementing the client surface the routes use."""

    def __init__(self) -> None:
        self.zones: dict[str, dict] = {}

    async def list_zones(self):
        return [
            dns.ZoneSummary(z["id"], z["name"], "Native", z["serial"]) for z in self.zones.values()
        ]

    async def get_zone(self, zone_id: str) -> dict:
        if zone_id not in self.zones:
            raise dns.ZoneNotFoundError(f"Zone not found: {zone_id}")
        return self.zones[zone_id]

    async def create_zone(self, name: str, nameservers: list[str]) -> dict:
        if name in self.zones:
            raise ConflictError(f"Zone {name} already exists")
        zone = {
            "id": name,
            "name": name,
            "serial": 1,
            "rrsets": [
                _soa(name),
                {
                    "name": name,
                    "type": "NS",
                    "ttl": 3600,
                    "records": [{"content": ns} for ns in nameservers],
                },
            ],
        }
        self.zones[name] = zone
        return zone

    async def delete_zone(self, zone_id: str) -> None:
        if zone_id not in self.zones:
            raise dns.ZoneNotFoundError(f"Zone not found: {zone_id}")
        del self.zones[zone_id]

    async def patch_rrsets(self, zone_id: str, patches: list[dict]) -> None:
        zone = await self.get_zone(zone_id)
        for p in patches:
            zone["rrsets"] = [
                r for r in zone["rrsets"] if not (r["name"] == p["name"] and r["type"] == p["type"])
            ]
            if p["changetype"] == "REPLACE":
                zone["rrsets"].append(
                    {"name": p["name"], "type": p["type"], "ttl": p["ttl"], "records": p["records"]}
                )
        zone["serial"] += 1


@pytest_asyncio.fixture
async def pdns(app):
    fake = FakePDNS()
    app.state.pdns_client = fake
    return fake


def _rr(body: dict) -> dict[tuple[str, str], dict]:
    return {(r["name"], r["type"]): r for r in body["rrsets"]}


async def test_dns_requires_auth(client, pdns):
    assert (await client.get("/api/dns/zones")).status_code == 401
    assert (await client.get("/api/dns/meta")).status_code == 401


async def test_meta_defaults(admin_client):
    resp = await admin_client.get("/api/dns/meta")
    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": True,
        "server_ip": "",
        "default_ttl": 3600,
        "cloudflare_enabled": False,
    }


async def test_zone_lifecycle(admin_client, pdns):
    # create normalizes the name and adds default self-hosted nameservers
    resp = await admin_client.post("/api/dns/zones", json={"name": "Example.COM"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "example.com."
    rr = _rr(body)
    assert rr[("example.com.", "NS")]["records"] == ["ns1.example.com.", "ns2.example.com."]

    assert [z["name"] for z in (await admin_client.get("/api/dns/zones")).json()] == [
        "example.com."
    ]

    # add a record (default TTL applied)
    resp = await admin_client.put(
        "/api/dns/zones/example.com./records",
        json={"name": "www", "type": "A", "records": ["192.0.2.10"]},
    )
    assert resp.status_code == 204
    rr = _rr((await admin_client.get("/api/dns/zones/example.com.")).json())
    assert rr[("www.example.com.", "A")]["records"] == ["192.0.2.10"]
    assert rr[("www.example.com.", "A")]["ttl"] == 3600

    # invalid payloads -> validation envelope
    resp = await admin_client.put(
        "/api/dns/zones/example.com./records",
        json={"name": "www", "type": "A", "records": ["not-an-ip"]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "dns_validation_error"
    resp = await admin_client.put(
        "/api/dns/zones/example.com./records",
        json={"name": "@", "type": "CNAME", "records": ["other.example.org"]},
    )
    assert resp.status_code == 422

    # delete the record
    resp = await admin_client.request(
        "DELETE",
        "/api/dns/zones/example.com./records",
        json={"name": "www", "type": "A"},
    )
    assert resp.status_code == 204
    rr = _rr((await admin_client.get("/api/dns/zones/example.com.")).json())
    assert ("www.example.com.", "A") not in rr

    # apex NS is protected
    resp = await admin_client.request(
        "DELETE", "/api/dns/zones/example.com./records", json={"name": "@", "type": "NS"}
    )
    assert resp.status_code == 409

    # zone deletion needs a matching confirmation
    resp = await admin_client.request(
        "DELETE", "/api/dns/zones/example.com.", json={"confirm_name": "wrong.com"}
    )
    assert resp.status_code == 409
    resp = await admin_client.request(
        "DELETE", "/api/dns/zones/example.com.", json={"confirm_name": "example.com"}
    )
    assert resp.status_code == 204
    assert (await admin_client.get("/api/dns/zones")).json() == []
    assert (await admin_client.get("/api/dns/zones/example.com.")).status_code == 404


async def test_create_duplicate_zone(admin_client, pdns):
    assert (await admin_client.post("/api/dns/zones", json={"name": "dup.com"})).status_code == 201
    resp = await admin_client.post("/api/dns/zones", json={"name": "dup.com"})
    assert resp.status_code == 409


async def test_create_invalid_zone_name(admin_client, pdns):
    resp = await admin_client.post("/api/dns/zones", json={"name": "not_a_domain"})
    assert resp.status_code == 422


async def test_point_to_server_requires_public_ip(admin_client, pdns):
    resp = await admin_client.post(
        "/api/dns/zones", json={"name": "example.com", "point_to_server": True}
    )
    assert resp.status_code == 409


async def test_templates(admin_client, pdns):
    await admin_client.post("/api/dns/zones", json={"name": "example.com"})

    resp = await admin_client.post("/api/dns/zones/example.com./templates/external-mail")
    assert resp.status_code == 204
    rr = _rr((await admin_client.get("/api/dns/zones/example.com.")).json())
    assert rr[("example.com.", "TXT")]["records"] == ['"v=spf1 mx ~all"']
    assert rr[("_dmarc.example.com.", "TXT")]["records"] == ['"v=DMARC1; p=none"']

    resp = await admin_client.post("/api/dns/zones/example.com./templates/google-workspace")
    assert resp.status_code == 204
    rr = _rr((await admin_client.get("/api/dns/zones/example.com.")).json())
    assert rr[("example.com.", "MX")]["records"] == ["1 smtp.google.com."]

    resp = await admin_client.post("/api/dns/zones/example.com./templates/does-not-exist")
    assert resp.status_code == 404


async def _logged_in_app(settings_obj):
    application = create_app(settings_obj)
    return application


async def test_point_to_server_with_public_ip(settings):
    s = settings.model_copy(update={"public_ip": "203.0.113.7"})
    application = create_app(s)
    async with application.router.lifespan_context(application):
        application.state.pdns_client = FakePDNS()
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            token = await setup_and_login(c)
            c.headers["Authorization"] = f"Bearer {token}"
            resp = await c.post(
                "/api/dns/zones", json={"name": "example.com", "point_to_server": True}
            )
            assert resp.status_code == 201, resp.text
            rr = _rr(resp.json())
            assert rr[("example.com.", "A")]["records"] == ["203.0.113.7"]
            assert rr[("www.example.com.", "CNAME")]["records"] == ["example.com."]


async def test_dns_disabled_hides_routes(settings):
    s = settings.model_copy(update={"dns_enabled": False})
    application = create_app(s)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            token = await setup_and_login(c)
            c.headers["Authorization"] = f"Bearer {token}"
            assert (await c.get("/api/dns/zones")).status_code == 404
            meta = await c.get("/api/dns/meta")
            assert meta.status_code == 200
            assert meta.json()["enabled"] is False


async def test_cloudflare_push_requires_token(admin_client, pdns):
    await admin_client.post("/api/dns/zones", json={"name": "example.com"})
    resp = await admin_client.post("/api/dns/zones/example.com./push/cloudflare")
    assert resp.status_code == 409
    assert "Cloudflare API token" in resp.json()["error"]["message"]


async def test_cloudflare_push_full_flow(settings):
    s = settings.model_copy(update={"cloudflare_api_token": "cf-token"})
    application = create_app(s)
    async with application.router.lifespan_context(application):
        application.state.pdns_client = FakePDNS()
        fake_cf = FakeCF(zone={"id": "cf-zone-1"})
        application.state.cloudflare_client = fake_cf
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            token = await setup_and_login(c)
            c.headers["Authorization"] = f"Bearer {token}"

            meta = await c.get("/api/dns/meta")
            assert meta.json()["cloudflare_enabled"] is True

            await c.post("/api/dns/zones", json={"name": "example.com"})
            await c.put(
                "/api/dns/zones/example.com./records",
                json={"name": "www", "type": "A", "records": ["192.0.2.10"]},
            )
            await c.put(
                "/api/dns/zones/example.com./records",
                json={"name": "@", "type": "MX", "records": ["10 mail.example.com"]},
            )

            resp = await c.post("/api/dns/zones/example.com./push/cloudflare")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            # SOA + apex NS skipped from push entirely; A + MX created
            assert body == {
                "zone": "example.com.",
                "created": 2,
                "updated": 0,
                "skipped": 0,
                "errors": [],
            }
            created = {(p["type"], p["name"]) for p in fake_cf.created}
            assert created == {("A", "www.example.com"), ("MX", "example.com")}


async def test_cloudflare_push_zone_missing_in_account(settings):
    s = settings.model_copy(update={"cloudflare_api_token": "cf-token"})
    application = create_app(s)
    async with application.router.lifespan_context(application):
        application.state.pdns_client = FakePDNS()
        application.state.cloudflare_client = FakeCF(zone=None)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            token = await setup_and_login(c)
            c.headers["Authorization"] = f"Bearer {token}"
            await c.post("/api/dns/zones", json={"name": "example.com"})
            resp = await c.post("/api/dns/zones/example.com./push/cloudflare")
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "cloudflare_zone_missing"


# --- create zone for a site (Sites UI bridge) --------------------------------------


@pytest_asyncio.fixture
async def fake_system(monkeypatch):
    from tests.test_sites_api import FakeSystem

    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


async def test_create_zone_for_site_assigns_site_owner(admin_client, client, pdns, fake_system):
    from tests.test_multi_tenancy import make_active_client

    headers = await make_active_client(admin_client, client, "alice")
    resp = await client.post("/api/sites", json={"domain": "client-site.example"}, headers=headers)
    assert resp.status_code == 202, resp.text
    site_id = resp.json()["site"]["id"]

    # The admin creates the zone on the client's behalf — ownership follows the site.
    resp = await admin_client.post(
        "/api/dns/zones", json={"name": "client-site.example", "site_id": site_id}
    )
    assert resp.status_code == 201, resp.text

    # The client sees (and can manage) the zone.
    zones = (await client.get("/api/dns/zones", headers=headers)).json()
    assert [z["name"] for z in zones] == ["client-site.example."]


async def test_create_zone_for_site_name_must_match(admin_client, pdns, fake_system):
    resp = await admin_client.post("/api/sites", json={"domain": "match.example"})
    assert resp.status_code == 202, resp.text
    site_id = resp.json()["site"]["id"]
    resp = await admin_client.post(
        "/api/dns/zones", json={"name": "other.example", "site_id": site_id}
    )
    assert resp.status_code == 409
    assert pdns.zones == {}


async def test_create_zone_for_other_tenants_site_is_404(admin_client, client, pdns, fake_system):
    from tests.test_multi_tenancy import make_active_client

    alice = await make_active_client(admin_client, client, "alice")
    resp = await client.post("/api/sites", json={"domain": "alices.example"}, headers=alice)
    assert resp.status_code == 202, resp.text
    site_id = resp.json()["site"]["id"]

    # Bob cannot piggyback on Alice's site — existence never leaks (404, not 403).
    bob = await make_active_client(admin_client, client, "bob")
    resp = await client.post(
        "/api/dns/zones",
        json={"name": "alices.example", "site_id": site_id},
        headers=bob,
    )
    assert resp.status_code == 404
    assert pdns.zones == {}


# --- pull from Cloudflare ------------------------------------------------------------


async def test_cloudflare_pull_full_flow(settings):
    s = settings.model_copy(update={"cloudflare_api_token": "cf-token"})
    application = create_app(s)
    async with application.router.lifespan_context(application):
        application.state.pdns_client = FakePDNS()
        fake_cf = FakeCF(
            zone={"id": "cf-zone-1"},
            existing=[
                {"type": "A", "name": "mail.example.com", "content": "84.46.251.171", "ttl": 1},
                {
                    "type": "MX",
                    "name": "example.com",
                    "content": "smtp.google.com",
                    "priority": 1,
                    "ttl": 1,
                },
                {"type": "TXT", "name": "example.com", "content": "v=spf1 ~all", "ttl": 3600},
                {"type": "NS", "name": "example.com", "content": "audrey.ns.cloudflare.com"},
            ],
        )
        application.state.cloudflare_client = fake_cf
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            token = await setup_and_login(c)
            c.headers["Authorization"] = f"Bearer {token}"
            await c.post("/api/dns/zones", json={"name": "example.com"})

            resp = await c.post("/api/dns/zones/example.com./pull/cloudflare")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            # A + MX + TXT imported; apex NS (Cloudflare's own) never touched.
            assert body == {
                "zone": "example.com.",
                "created": 3,
                "updated": 0,
                "skipped": 0,
                "errors": [],
            }
            rr = _rr((await c.get("/api/dns/zones/example.com.")).json())
            assert rr[("mail.example.com.", "A")]["records"] == ["84.46.251.171"]
            assert rr[("example.com.", "MX")]["records"] == ["1 smtp.google.com."]
            assert rr[("example.com.", "TXT")]["records"] == ['"v=spf1 ~all"']
            # The panel zone keeps its own (self-hosted-style) NS set.
            assert rr[("example.com.", "NS")]["records"] == [
                "ns1.example.com.",
                "ns2.example.com.",
            ]

            # Idempotent: a second pull changes nothing.
            body = (await c.post("/api/dns/zones/example.com./pull/cloudflare")).json()
            assert body["created"] == 0 and body["updated"] == 0 and body["skipped"] == 3


async def test_cloudflare_pull_zone_missing_in_account(settings):
    s = settings.model_copy(update={"cloudflare_api_token": "cf-token"})
    application = create_app(s)
    async with application.router.lifespan_context(application):
        application.state.pdns_client = FakePDNS()
        application.state.cloudflare_client = FakeCF(zone=None)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            token = await setup_and_login(c)
            c.headers["Authorization"] = f"Bearer {token}"
            await c.post("/api/dns/zones", json={"name": "example.com"})
            resp = await c.post("/api/dns/zones/example.com./pull/cloudflare")
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "cloudflare_zone_missing"


# --- delegation detection ------------------------------------------------------------


def _fake_doh(ns: list[str], ips: list[str]):
    async def doh(name: str, rtype: str) -> list[str]:
        return ns if rtype == "NS" else ips

    return doh


async def test_delegation_unresolved(admin_client, pdns, app):
    await admin_client.post("/api/dns/zones", json={"name": "example.com"})
    app.state.doh_query = _fake_doh([], [])
    body = (await admin_client.get("/api/dns/zones/example.com./delegation")).json()
    assert body["points_here"] is None
    assert body["live_nameservers"] == []
    assert "could not be resolved" in body["detail"]


async def test_delegation_elsewhere_and_here(settings):
    s = settings.model_copy(update={"public_ip": "192.0.2.7"})
    application = create_app(s)
    async with application.router.lifespan_context(application):
        application.state.pdns_client = FakePDNS()
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            token = await setup_and_login(c)
            c.headers["Authorization"] = f"Bearer {token}"
            await c.post("/api/dns/zones", json={"name": "example.com"})

            # Registrar points at Cloudflare, whose nameservers are NOT this IP.
            application.state.doh_query = _fake_doh(
                ["audrey.ns.cloudflare.com.", "clyde.ns.cloudflare.com."], ["104.16.1.1"]
            )
            body = (await c.get("/api/dns/zones/example.com./delegation")).json()
            assert body["points_here"] is False
            assert "NOT live" in body["detail"]
            assert "audrey.ns.cloudflare.com" in body["detail"]

            # Registrar nameservers resolve to this server -> zone is live.
            application.state.doh_query = _fake_doh(["ns1.example.com."], ["192.0.2.7"])
            body = (await c.get("/api/dns/zones/example.com./delegation")).json()
            assert body["points_here"] is True
            assert "live" in body["detail"]


async def test_delegation_without_public_ip_lists_nameservers(admin_client, pdns, app):
    await admin_client.post("/api/dns/zones", json={"name": "example.com"})
    app.state.doh_query = _fake_doh(["audrey.ns.cloudflare.com."], ["104.16.1.1"])
    body = (await admin_client.get("/api/dns/zones/example.com./delegation")).json()
    # Test settings have no HOSTY_PUBLIC_IP -> the panel cannot give a verdict.
    assert body["points_here"] is None
    assert "audrey.ns.cloudflare.com" in body["detail"]


async def test_apex_ns_is_read_only(admin_client, pdns):
    await admin_client.post("/api/dns/zones", json={"name": "example.com"})
    resp = await admin_client.put(
        "/api/dns/zones/example.com./records",
        json={"name": "@", "type": "NS", "records": ["audrey.ns.cloudflare.com"]},
    )
    assert resp.status_code == 409
    assert "registrar" in resp.json()["error"]["message"]
    # Subdomain delegation NS records remain allowed.
    resp = await admin_client.put(
        "/api/dns/zones/example.com./records",
        json={"name": "sub", "type": "NS", "records": ["ns1.elsewhere.example"]},
    )
    assert resp.status_code == 204
