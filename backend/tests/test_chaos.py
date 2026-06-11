"""Chaos cases (Week 23): infrastructure dies mid-operation — the panel must
fail gracefully, roll back, leave no corrupt state, and allow a retry."""

from __future__ import annotations

import errno

import pytest

from app.services import mariadb, wordpress
from app.services.caddy import CaddyClient, CaddyError
from tests.test_backups_api import env  # noqa: F401  (fixture reuse)
from tests.test_sites_api import FakeSystem


@pytest.fixture
def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


async def _op(client, op_id: int) -> dict:
    return (await client.get(f"/api/operations/{op_id}")).json()


# --- Caddy admin API down ----------------------------------------------------------


async def test_caddy_down_during_create_rolls_back_everything(
    admin_client, fake_system, monkeypatch
):
    async def caddy_down(self, config):
        raise CaddyError("Caddy Admin API unreachable: ConnectError")

    monkeypatch.setattr(CaddyClient, "apply", caddy_down)
    resp = await admin_client.post("/api/sites", json={"domain": "caddy-down.example"})
    op = await _op(admin_client, resp.json()["operation_id"])

    assert op["status"] == "failed"
    assert "unreachable" in op["error"]
    # Nothing half-provisioned survives.
    assert fake_system.linux_users == set()
    assert fake_system.pools == set()
    site = (await admin_client.get(f"/api/sites/{resp.json()['site']['id']}")).json()
    assert site["status"] == "error"


async def test_caddy_down_during_delete_is_retryable(admin_client, fake_system, monkeypatch):
    created = (await admin_client.post("/api/sites", json={"domain": "del-retry.example"})).json()
    site_id = created["site"]["id"]

    real_apply = CaddyClient.apply

    async def caddy_down(self, config):
        raise CaddyError("Caddy Admin API unreachable")

    monkeypatch.setattr(CaddyClient, "apply", caddy_down)
    resp = await admin_client.request(
        "DELETE", f"/api/sites/{site_id}", json={"confirm_domain": "del-retry.example"}
    )
    op = await _op(admin_client, resp.json()["operation_id"])
    assert op["status"] == "failed"
    site = (await admin_client.get(f"/api/sites/{site_id}")).json()
    assert site["status"] == "error"  # flagged, not stuck in "deleting"

    # Caddy comes back: the delete simply runs again (idempotent steps).
    monkeypatch.setattr(CaddyClient, "apply", real_apply)
    resp = await admin_client.request(
        "DELETE", f"/api/sites/{site_id}", json={"confirm_domain": "del-retry.example"}
    )
    op = await _op(admin_client, resp.json()["operation_id"])
    assert op["status"] == "succeeded"
    assert (await admin_client.get(f"/api/sites/{site_id}")).status_code == 404


# --- MariaDB dies mid-provision ----------------------------------------------------


@pytest.fixture
def fake_wp(monkeypatch):
    state = {"fail_on": None, "calls": []}

    async def fake_run_wp(user, root, args, timeout=120.0, check=True):
        from app.system.runner import CommandResult

        state["calls"].append(list(args))
        ok = not (state["fail_on"] and args[: len(state["fail_on"])] == state["fail_on"])
        if args[:2] == ["core", "is-installed"]:
            ok = False
        if check and not ok:
            raise wordpress.WordPressError(f"wp {args[0]} failed: server has gone away")
        return CommandResult(("wp", *args), 0 if ok else 1, "", "", 1.0)

    monkeypatch.setattr(wordpress, "run_wp", fake_run_wp)
    return state


WP_BODY = {
    "title": "Chaos Blog",
    "admin_user": "admin",
    "admin_password": "a-strong-password",
    "admin_email": "don@example.com",
}


async def test_mariadb_dies_mid_wordpress_install(admin_client, fake_system, fake_wp):
    """DB provisioned, core downloaded, then MariaDB dies during `core install`:
    the database is dropped, files restored to the skeleton, site unharmed."""
    site_id = ((await admin_client.post("/api/sites", json={"domain": "db-dies.example"})).json())[
        "site"
    ]["id"]
    fake_wp["fail_on"] = ["core", "install"]

    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress", json=WP_BODY)
    op = await _op(admin_client, resp.json()["operation_id"])

    assert op["status"] == "failed"
    statuses = {s["name"]: s["status"] for s in op["steps"]}
    assert statuses["install"] == "failed"
    assert statuses["database"] == "rolled_back"
    assert statuses["download"] == "rolled_back"
    assert fake_system.databases == set()  # dropped, not orphaned

    site = (await admin_client.get(f"/api/sites/{site_id}")).json()
    assert site["status"] == "active" and site["wordpress"] is False


async def test_mariadb_down_at_database_step(admin_client, fake_system, fake_wp, monkeypatch):
    async def db_down(database, user, password):
        raise mariadb.MariaDBError("create database failed: Can't connect to server")

    monkeypatch.setattr(mariadb, "create_database", db_down)
    site_id = ((await admin_client.post("/api/sites", json={"domain": "db-down.example"})).json())[
        "site"
    ]["id"]

    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress", json=WP_BODY)
    op = await _op(admin_client, resp.json()["operation_id"])
    assert op["status"] == "failed"
    statuses = {s["name"]: s["status"] for s in op["steps"]}
    assert statuses["database"] == "failed"
    # Nothing after the failed first step ever ran.
    assert statuses["download"] == "pending"
    assert all(c[:2] != ["core", "download"] for c in fake_wp["calls"])


# --- disk full during backup --------------------------------------------------------


async def test_disk_full_during_backup(env, monkeypatch):  # noqa: F811 (fixture)
    """ENOSPC mid-archive: the operation fails cleanly and the half-written
    backup is not listed as restorable (no manifest, no checksums)."""

    async def disk_full(site_dir, dest):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("app.services.backup_ops.backup.archive_files", disk_full)

    client, site_id = env["client"], env["site_id"]
    resp = await client.post(f"/api/sites/{site_id}/backups")
    assert resp.status_code == 202
    from tests.test_backups_api import _poll_operation

    op = await _poll_operation(client, resp.json()["operation_id"])
    assert op["status"] == "failed"
    assert "No space left" in op["error"]

    listing = (await client.get("/api/backups")).json()
    assert listing == []  # the partial backup is not offered for restore
