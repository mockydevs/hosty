"""Cloudflare account management API: token config + zone/record CRUD.

The Cloudflare HTTP client is faked via `app.state.cloudflare_client`; the
token storage path (encrypted PanelSetting) is exercised for real.
"""

from __future__ import annotations

from app.services.cloudflare import CloudflareError
from tests.conftest import setup_and_login  # noqa: F401  (fixture file)
from tests.test_cloudflare import FakeCF


class FakeCFAccount(FakeCF):
    """FakeCF extended with the account-management surface."""

    def __init__(self, zones=None, existing=None, verify_ok=True):
        super().__init__(existing=existing)
        self.zones = zones or []
        self.verify_ok = verify_ok
        self.deleted: list[tuple[str, str]] = []

    async def verify_token(self):
        if not self.verify_ok:
            raise CloudflareError("Cloudflare API error: Invalid API Token")
        return {"id": "tok-1", "status": "active"}

    async def list_zones(self):
        return self.zones

    async def delete_record(self, zone_id, record_id):
        self.deleted.append((zone_id, record_id))


ZONE = {
    "id": "cf-zone-1",
    "name": "example.com",
    "status": "active",
    "paused": False,
    "name_servers": ["ana.ns.cloudflare.com", "bob.ns.cloudflare.com"],
}

RECORD = {
    "id": "rec-1",
    "type": "A",
    "name": "www.example.com",
    "content": "192.0.2.10",
    "ttl": 300,
    "proxied": True,
}


async def test_cloudflare_config_requires_auth(client):
    assert (await client.get("/api/dns/cloudflare/config")).status_code == 401
    assert (await client.get("/api/dns/cloudflare/zones")).status_code == 401


async def test_token_save_verify_and_clear(admin_client, app):
    # Nothing configured initially.
    resp = await admin_client.get("/api/dns/cloudflare/config")
    assert resp.json() == {"configured": False, "source": None}

    # Bad token: verification fails, nothing stored.
    app.state.cloudflare_client = FakeCFAccount(verify_ok=False)
    resp = await admin_client.put("/api/dns/cloudflare/config", json={"api_token": "bad-token-123"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "cloudflare_error"
    assert (await admin_client.get("/api/dns/cloudflare/config")).json()["configured"] is False

    # Good token: verified, stored encrypted, surfaced in meta.
    app.state.cloudflare_client = FakeCFAccount()
    resp = await admin_client.put(
        "/api/dns/cloudflare/config", json={"api_token": "good-token-123"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"configured": True, "source": "db"}
    meta = (await admin_client.get("/api/dns/meta")).json()
    assert meta["cloudflare_enabled"] is True

    # Clear.
    assert (await admin_client.delete("/api/dns/cloudflare/config")).status_code == 204
    assert (await admin_client.get("/api/dns/cloudflare/config")).json()["configured"] is False
    assert (await admin_client.delete("/api/dns/cloudflare/config")).status_code == 404


async def test_zone_and_record_endpoints_require_token(admin_client):
    resp = await admin_client.get("/api/dns/cloudflare/zones")
    assert resp.status_code == 409
    assert "Cloudflare API token" in resp.json()["error"]["message"]


async def test_list_zones_and_records(admin_client, app):
    app.state.cloudflare_client = FakeCFAccount(zones=[ZONE], existing=[RECORD])

    zones = (await admin_client.get("/api/dns/cloudflare/zones")).json()
    assert zones == [
        {
            "id": "cf-zone-1",
            "name": "example.com",
            "status": "active",
            "paused": False,
            "name_servers": ["ana.ns.cloudflare.com", "bob.ns.cloudflare.com"],
        }
    ]

    records = (await admin_client.get("/api/dns/cloudflare/zones/cf-zone-1/records")).json()
    assert records == [
        {
            "id": "rec-1",
            "type": "A",
            "name": "www.example.com",
            "content": "192.0.2.10",
            "ttl": 300,
            "proxied": True,
            "priority": None,
        }
    ]


async def test_record_create_update_delete(admin_client, app):
    fake = FakeCFAccount(zones=[ZONE])
    app.state.cloudflare_client = fake

    # Create: proxied flag kept for A records, trailing dot stripped from name.
    resp = await admin_client.post(
        "/api/dns/cloudflare/zones/cf-zone-1/records",
        json={"type": "A", "name": "www.example.com.", "content": "192.0.2.20", "proxied": True},
    )
    assert resp.status_code == 204, resp.text
    assert fake.created == [
        {
            "type": "A",
            "name": "www.example.com",
            "content": "192.0.2.20",
            "ttl": 1,
            "proxied": True,
        }
    ]

    # Update: MX gets a default priority and no proxied flag.
    resp = await admin_client.put(
        "/api/dns/cloudflare/zones/cf-zone-1/records/rec-9",
        json={"type": "MX", "name": "example.com", "content": "mail.example.com", "ttl": 3600},
    )
    assert resp.status_code == 204, resp.text
    record_id, payload = fake.updated[0]
    assert record_id == "rec-9"
    assert payload == {
        "type": "MX",
        "name": "example.com",
        "content": "mail.example.com",
        "ttl": 3600,
        "priority": 10,
    }

    # Delete.
    resp = await admin_client.delete("/api/dns/cloudflare/zones/cf-zone-1/records/rec-1")
    assert resp.status_code == 204
    assert fake.deleted == [("cf-zone-1", "rec-1")]


async def test_record_type_validation(admin_client, app):
    app.state.cloudflare_client = FakeCFAccount(zones=[ZONE])
    resp = await admin_client.post(
        "/api/dns/cloudflare/zones/cf-zone-1/records",
        json={"type": "SRV", "name": "x", "content": "y"},
    )
    assert resp.status_code == 422
