"""Plans (Phase 11d): CRUD, in-use protection, and quota inheritance."""

from __future__ import annotations

import pytest_asyncio

from tests.test_multi_tenancy import make_active_client
from tests.test_sites_api import FakeSystem

STARTER = {"name": "Starter", "max_sites": 1, "max_databases": 1, "max_disk_mb": 1024}


@pytest_asyncio.fixture
async def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


async def _create_plan(admin_client, **overrides) -> dict:
    resp = await admin_client.post("/api/plans", json={**STARTER, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_plan_crud(admin_client):
    plan = await _create_plan(admin_client)
    assert plan["name"] == "Starter" and plan["user_count"] == 0

    # Duplicate name refused (case-exact, trimmed).
    resp = await admin_client.post("/api/plans", json={**STARTER, "name": " Starter "})
    assert resp.status_code == 409

    resp = await admin_client.put(
        f"/api/plans/{plan['id']}", json={**STARTER, "name": "Starter v2", "max_sites": 2}
    )
    assert resp.status_code == 200 and resp.json()["max_sites"] == 2

    listed = (await admin_client.get("/api/plans")).json()
    assert [p["name"] for p in listed] == ["Starter v2"]

    assert (await admin_client.delete(f"/api/plans/{plan['id']}")).status_code == 204
    assert (await admin_client.get("/api/plans")).json() == []
    assert (await admin_client.delete(f"/api/plans/{plan['id']}")).status_code == 404


async def test_plan_delete_refused_while_assigned(admin_client, client):
    plan = await _create_plan(admin_client)
    await make_active_client(admin_client, client, plan_id=plan["id"])

    listed = (await admin_client.get("/api/plans")).json()
    assert listed[0]["user_count"] == 1

    resp = await admin_client.delete(f"/api/plans/{plan['id']}")
    assert resp.status_code == 409
    assert "reassign" in resp.json()["error"]["message"]


async def test_create_user_with_unknown_plan_404s(admin_client):
    resp = await admin_client.post("/api/users", json={"username": "bob", "plan_id": 999})
    assert resp.status_code == 404


async def test_plan_quota_enforced_on_site_create(admin_client, client, fake_system):
    plan = await _create_plan(admin_client, max_sites=0)
    headers = await make_active_client(admin_client, client, plan_id=plan["id"])

    resp = await client.post("/api/sites", json={"domain": "blocked.example"}, headers=headers)
    assert resp.status_code == 409
    assert "quota" in resp.json()["error"]["message"].lower()


async def test_explicit_user_limit_overrides_plan_on_create(admin_client, client, fake_system):
    plan = await _create_plan(admin_client, max_sites=0)
    headers = await make_active_client(admin_client, client, plan_id=plan["id"], max_sites=1)
    resp = await client.post("/api/sites", json={"domain": "allowed.example"}, headers=headers)
    assert resp.status_code == 202, resp.text


async def test_clear_plan_restores_unlimited(admin_client, client, fake_system):
    plan = await _create_plan(admin_client, max_sites=0)
    headers = await make_active_client(admin_client, client, plan_id=plan["id"])
    users = (await admin_client.get("/api/users")).json()
    alice = next(u for u in users if u["username"] == "alice")
    assert alice["plan_name"] == "Starter"

    resp = await admin_client.patch(f"/api/users/{alice['id']}", json={"clear_plan": True})
    assert resp.status_code == 200 and resp.json()["plan_id"] is None

    resp = await client.post("/api/sites", json={"domain": "freed.example"}, headers=headers)
    assert resp.status_code == 202, resp.text


async def test_plans_are_admin_only(admin_client, client):
    headers = await make_active_client(admin_client, client)
    assert (await client.get("/api/plans", headers=headers)).status_code == 403
    assert (await client.post("/api/plans", json=STARTER, headers=headers)).status_code == 403
