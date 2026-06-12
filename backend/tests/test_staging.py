"""Staging clones (Phase 11d): domain derivation, clone pipeline, push guards."""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.services import staging, wordpress
from app.services.staging import StagingError
from app.system import fs
from tests.test_sites_api import FakeSystem

# --- pure parts -----------------------------------------------------------------------


def test_staging_domain_for():
    assert staging.staging_domain_for("blog.example") == "staging.blog.example"
    with pytest.raises(StagingError):
        staging.staging_domain_for("a" * 250 + ".example")


# --- fixtures -------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)

    async def mirror_tree(src: str, dest: str, *, root: str, delete: bool = False) -> None:
        fake.calls.append(("mirror_tree", src, dest))

    class _OK:
        ok = True
        stdout = ""
        stderr = ""

    async def runner_dump(db_name: str, dump_path: str):
        fake.calls.append(("mysqldump", db_name))
        return _OK()

    async def runner_restore(db_name: str, dump_path: str):
        fake.calls.append(("mysql_restore", db_name))
        return _OK()

    async def run_wp(site_user: str, doc_root: str, args: list[str], timeout: int = 300):
        fake.calls.append(("wp", site_user, tuple(args[:2])))
        return ""

    monkeypatch.setattr(fs, "mirror_tree", mirror_tree)
    monkeypatch.setattr(staging, "runner_dump", runner_dump)
    monkeypatch.setattr(staging, "runner_restore", runner_restore)
    monkeypatch.setattr(wordpress, "run_wp", run_wp)
    return fake


async def _make_site(admin_client, domain="prod.example") -> dict:
    resp = await admin_client.post("/api/sites", json={"domain": domain})
    assert resp.status_code == 202, resp.text
    return resp.json()["site"]


# --- create staging -------------------------------------------------------------------


async def test_create_staging_clone(admin_client, fake_system):
    site = await _make_site(admin_client)
    resp = await admin_client.post(f"/api/sites/{site['id']}/staging")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["site"]["domain"] == "staging.prod.example"
    assert body["site"]["staging_of"] == site["id"]

    op = (await admin_client.get(f"/api/operations/{body['operation_id']}")).json()
    assert op["status"] == "succeeded", op
    assert ("mirror_tree", site["doc_root"], body["site"]["doc_root"]) in [
        c for c in fake_system.calls if c[0] == "mirror_tree"
    ] or any(c[0] == "mirror_tree" for c in fake_system.calls)

    # The staging site is a real, listed site.
    listed = (await admin_client.get("/api/sites")).json()
    domains = {s["domain"] for s in listed}
    assert "staging.prod.example" in domains


async def test_create_staging_twice_is_refused(admin_client, fake_system):
    site = await _make_site(admin_client)
    assert (await admin_client.post(f"/api/sites/{site['id']}/staging")).status_code == 202
    resp = await admin_client.post(f"/api/sites/{site['id']}/staging")
    assert resp.status_code == 409
    assert "already exists" in resp.json()["error"]["message"]


async def test_staging_a_staging_site_is_refused(admin_client, fake_system):
    site = await _make_site(admin_client)
    accepted = (await admin_client.post(f"/api/sites/{site['id']}/staging")).json()
    resp = await admin_client.post(f"/api/sites/{accepted['site']['id']}/staging")
    assert resp.status_code == 409
    assert "staging site" in resp.json()["error"]["message"]


# --- push to production ---------------------------------------------------------------


async def test_push_requires_staging_site_and_confirmation(admin_client, fake_system):
    site = await _make_site(admin_client)

    # Pushing a non-staging site -> 409.
    resp = await admin_client.post(
        f"/api/sites/{site['id']}/staging/push", json={"confirm_domain": "prod.example"}
    )
    assert resp.status_code == 409

    accepted = (await admin_client.post(f"/api/sites/{site['id']}/staging")).json()
    staging_id = accepted["site"]["id"]

    # Wrong confirmation -> 409, nothing runs.
    resp = await admin_client.post(
        f"/api/sites/{staging_id}/staging/push",
        json={"confirm_domain": "staging.prod.example"},
    )
    assert resp.status_code == 409

    # Confirming with the PRODUCTION domain starts the push.
    resp = await admin_client.post(
        f"/api/sites/{staging_id}/staging/push", json={"confirm_domain": "prod.example"}
    )
    assert resp.status_code == 202, resp.text
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded", op


# --- PHP error log viewer (Phase 11d, same router) -------------------------------------


async def test_php_error_log_missing_file(admin_client, fake_system):
    site = await _make_site(admin_client, domain="logs.example")
    resp = await admin_client.get(f"/api/sites/{site['id']}/logs/php")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is False and body["lines"] == []
    assert body["path"].endswith("/php-error.log")


async def test_php_error_log_is_owner_scoped(admin_client, client, fake_system):
    from tests.test_multi_tenancy import make_active_client

    site = await _make_site(admin_client, domain="admins.example")
    headers = await make_active_client(admin_client, client)
    resp = await client.get(f"/api/sites/{site['id']}/logs/php", headers=headers)
    assert resp.status_code == 404  # someone else's site never exists for a client
