"""Week 17: auto-create a PowerDNS zone (SOA/NS + A -> server IP) on site create."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import setup_and_login
from tests.test_dns_api import FakePDNS
from tests.test_sites_api import FakeSystem


@pytest_asyncio.fixture
async def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


@pytest_asyncio.fixture
async def pdns(app) -> FakePDNS:
    fake = FakePDNS()
    app.state.pdns_client = fake
    return fake


def _rrsets(zone: dict) -> dict[tuple[str, str], dict]:
    return {(r["name"], r["type"]): r for r in zone["rrsets"]}


async def test_create_site_without_dns_zone_by_default(admin_client, fake_system, pdns):
    resp = await admin_client.post("/api/sites", json={"domain": "plain.example"})
    assert resp.status_code == 202
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded"
    assert "dns_zone" not in {s["name"] for s in op["steps"]}
    assert pdns.zones == {}


async def test_create_site_with_dns_zone(admin_client, fake_system, pdns):
    resp = await admin_client.post(
        "/api/sites", json={"domain": "withdns.example", "create_dns_zone": True}
    )
    assert resp.status_code == 202
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded"
    statuses = {s["name"]: s["status"] for s in op["steps"]}
    assert statuses["dns_zone"] == "done"

    zone = pdns.zones["withdns.example."]
    rr = _rrsets(zone)
    assert ("withdns.example.", "SOA") in rr
    assert ("withdns.example.", "NS") in rr
    # No HOSTY_PUBLIC_IP in the test settings -> no A/www records.
    assert ("withdns.example.", "A") not in rr


def _settings_with_ip() -> Settings:
    return Settings(
        env="test",
        secret_key="test-secret-key-at-least-32-bytes-long!",
        database_url="sqlite+aiosqlite:///:memory:",
        create_tables_on_startup=True,
        cookie_secure=False,
        public_ip="203.0.113.7",
        _env_file=None,
    )


async def test_create_site_with_dns_zone_points_to_server(fake_system):
    application = create_app(_settings_with_ip())
    application.state.pdns_client = FakePDNS()
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = await setup_and_login(client)
            client.headers["Authorization"] = f"Bearer {token}"
            resp = await client.post(
                "/api/sites", json={"domain": "pointed.example", "create_dns_zone": True}
            )
            assert resp.status_code == 202
            op = (await client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
            assert op["status"] == "succeeded"

    rr = _rrsets(application.state.pdns_client.zones["pointed.example."])
    a = rr[("pointed.example.", "A")]
    assert [r["content"] for r in a["records"]] == ["203.0.113.7"]
    www = rr[("www.pointed.example.", "CNAME")]
    assert [r["content"] for r in www["records"]] == ["pointed.example."]


async def test_existing_zone_is_kept_and_step_succeeds(admin_client, fake_system, pdns):
    await pdns.create_zone("existing.example.", ["ns1.existing.example."])
    serial_before = pdns.zones["existing.example."]["serial"]

    resp = await admin_client.post(
        "/api/sites", json={"domain": "existing.example", "create_dns_zone": True}
    )
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded"
    # Idempotent skip: the pre-existing zone was not touched.
    assert pdns.zones["existing.example."]["serial"] == serial_before


async def test_zone_rolled_back_when_dns_step_fails(fake_system):
    """Zone create succeeds but the follow-up record patch fails: the step
    fails, earlier steps roll back, and the zone we just created is deleted
    (a pre-existing zone would have been kept — see the skip test above)."""

    class ExplodingPDNS(FakePDNS):
        async def patch_rrsets(self, zone_id, patches):
            raise RuntimeError("pdns down mid-create")

    exploding = ExplodingPDNS()
    # Records are only patched when public_ip is set; build an app with it.
    application = create_app(_settings_with_ip())
    application.state.pdns_client = exploding
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = await setup_and_login(client)
            client.headers["Authorization"] = f"Bearer {token}"
            resp = await client.post(
                "/api/sites", json={"domain": "rollback.example", "create_dns_zone": True}
            )
            op = (await client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
            assert op["status"] == "failed"
            statuses = {st["name"]: st["status"] for st in op["steps"]}
            assert statuses["dns_zone"] == "failed"
            assert statuses["linux_user"] == "rolled_back"
    # The zone we created during the failed run was deleted again.
    assert exploding.zones == {}


async def test_dns_zone_toggle_requires_dns_enabled(admin_client, fake_system, app):
    app.state.settings.dns_enabled = False
    try:
        resp = await admin_client.post(
            "/api/sites", json={"domain": "nodns.example", "create_dns_zone": True}
        )
        assert resp.status_code == 409
    finally:
        app.state.settings.dns_enabled = True


@pytest.mark.usefixtures("fake_system", "pdns")
async def test_dns_zone_toggle_is_admin_only(admin_client, client):
    from tests.test_multi_tenancy import make_active_client

    headers = await make_active_client(admin_client, client)
    resp = await client.post(
        "/api/sites",
        json={"domain": "clientdns.example", "create_dns_zone": True},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "administrator" in resp.text.lower()
