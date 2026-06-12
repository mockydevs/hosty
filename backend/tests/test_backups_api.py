from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db.models import Site
from app.main import create_app
from app.services import backup, scheduler
from tests.conftest import setup_and_login
from tests.test_backup import _make_backup_dir, make_fake_runner


@pytest_asyncio.fixture
async def env(settings, tmp_path, monkeypatch):
    """App with a tmp backups root, an active site row, and a faked runner."""
    calls: list[list[str]] = []
    monkeypatch.setattr("app.system.runner.run", make_fake_runner(calls))

    s = settings.model_copy(
        update={
            "backups_root": str(tmp_path / "backups"),
            "restore_staging_root": str(tmp_path / "restore-staging"),
        }
    )
    application = create_app(s)
    async with application.router.lifespan_context(application):
        async with application.state.sessionmaker() as db:
            site = Site(
                domain="example.com",
                site_user="site-example-com-a1b2c3",
                doc_root="/var/www/example.com/public_html",
                php_version="8.3",
                status="active",
            )
            db.add(site)
            await db.commit()
            await db.refresh(site)
            site_id = site.id
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = await setup_and_login(client)
            client.headers["Authorization"] = f"Bearer {token}"
            yield {
                "app": application,
                "client": client,
                "site_id": site_id,
                "settings": s,
                "calls": calls,
            }


async def _poll_operation(client: AsyncClient, operation_id: int) -> dict:
    resp = await client.get(f"/api/operations/{operation_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_backups_require_auth(client):
    assert (await client.get("/api/backups")).status_code == 401


async def test_meta_and_empty_list(env):
    client = env["client"]
    meta = (await client.get("/api/backups/meta")).json()
    assert meta == {"s3_enabled": False, "scheduler_enabled": True}
    assert (await client.get("/api/backups")).json() == []
    assert (await client.get(f"/api/sites/{env['site_id']}/backups")).json() == []


async def test_backup_then_restore_full_cycle(env):
    client, site_id = env["client"], env["site_id"]

    # run a backup (background task completes before the next request)
    resp = await client.post(f"/api/sites/{site_id}/backups")
    assert resp.status_code == 202, resp.text
    op = await _poll_operation(client, resp.json()["operation_id"])
    assert op["status"] == "succeeded", op
    assert [s["status"] for s in op["steps"]] == ["done"] * len(op["steps"])

    backups = (await client.get(f"/api/sites/{site_id}/backups")).json()
    assert len(backups) == 1
    backup_id = backups[0]["backup_id"]
    assert backups[0]["domain"] == "example.com"
    assert backups[0]["s3"] is False
    assert backups[0]["size_bytes"] > 0

    # restore: wrong confirmation -> 409; right -> isolated extraction + mirror
    resp = await client.post(
        f"/api/sites/{site_id}/backups/{backup_id}/restore",
        json={"scope": "full", "confirm_domain": "wrong.com"},
    )
    assert resp.status_code == 409
    resp = await client.post(
        f"/api/sites/{site_id}/backups/{backup_id}/restore",
        json={"scope": "full", "confirm_domain": "example.com"},
    )
    assert resp.status_code == 202
    op = await _poll_operation(client, resp.json()["operation_id"])
    assert op["status"] == "succeeded", op
    assert any(call[0] == "systemd-run" for call in env["calls"])
    assert any(call[0] == "chown" for call in env["calls"])

    # delete with confirmation
    resp = await client.request(
        "DELETE",
        f"/api/sites/{site_id}/backups/{backup_id}",
        json={"confirm_id": "nope"},
    )
    assert resp.status_code == 409
    resp = await client.request(
        "DELETE",
        f"/api/sites/{site_id}/backups/{backup_id}",
        json={"confirm_id": backup_id},
    )
    assert resp.status_code == 204
    assert (await client.get(f"/api/sites/{site_id}/backups")).json() == []


async def test_restore_unknown_backup_404(env):
    resp = await env["client"].post(
        f"/api/sites/{env['site_id']}/backups/20200101T000000Z/restore",
        json={"scope": "full", "confirm_domain": "example.com"},
    )
    assert resp.status_code == 404


async def test_backup_unknown_site_404(env):
    assert (await env["client"].post("/api/sites/999/backups")).status_code == 404


async def test_schedule_roundtrip_and_validation(env):
    client, site_id = env["client"], env["site_id"]
    schedule = (await client.get(f"/api/sites/{site_id}/backup-schedule")).json()
    assert schedule == {
        "enabled": False,
        "frequency": "daily",
        "hour": 3,
        "retention": 7,
        "include_files": True,
        "include_databases": True,
        "s3_mirror": True,
        "last_run_at": None,
    }
    resp = await client.put(
        f"/api/sites/{site_id}/backup-schedule",
        json={"enabled": True, "frequency": "weekly", "hour": 5, "retention": 3},
    )
    assert resp.status_code == 200
    assert resp.json()["frequency"] == "weekly"
    assert (await client.get(f"/api/sites/{site_id}/backup-schedule")).json()["enabled"] is True

    resp = await client.put(
        f"/api/sites/{site_id}/backup-schedule",
        json={"enabled": True, "frequency": "daily", "hour": 25, "retention": 3},
    )
    assert resp.status_code == 422


async def test_scheduler_tick_runs_due_backups_once(env):
    client, site_id, app = env["client"], env["site_id"], env["app"]
    await client.put(
        f"/api/sites/{site_id}/backup-schedule",
        json={"enabled": True, "frequency": "daily", "hour": 0, "retention": 7},
    )
    assert await scheduler.tick(app) == 1  # due (never ran) -> backup created
    backups = (await client.get(f"/api/sites/{site_id}/backups")).json()
    assert len(backups) == 1
    assert await scheduler.tick(app) == 0  # already ran in this slot

    schedule = (await client.get(f"/api/sites/{site_id}/backup-schedule")).json()
    assert schedule["last_run_at"] is not None


async def test_restore_from_s3_when_local_missing(env, tmp_path):
    """A backup that exists only in S3 is fetched, verified, and restored."""
    from tests.test_backup import FakeS3

    client, site_id, app, s = env["client"], env["site_id"], env["app"], env["settings"]
    s3 = FakeS3()
    app.state.s3_client = s3

    # seed S3 with a valid backup made elsewhere
    source = tmp_path / "elsewhere" / "example.com" / "20260611T030000Z"
    _make_backup_dir(source, databases=[])
    await backup.upload_backup(s3, "hosty", source, "example.com", "20260611T030000Z")

    resp = await client.post(
        f"/api/sites/{site_id}/backups/20260611T030000Z/restore",
        json={"scope": "files", "confirm_domain": "example.com"},
    )
    assert resp.status_code == 202, resp.text
    op = await _poll_operation(client, resp.json()["operation_id"])
    assert op["status"] == "succeeded", op
    # fetched into the local store
    local = backup.list_backups(s.backups_root, "example.com")
    assert [b.backup_id for b in local] == ["20260611T030000Z"]


# --- UI-managed S3 configuration -------------------------------------------------------

S3_BODY = {
    "endpoint": "https://s3.eu-central-1.amazonaws.com",
    "bucket": "hosty-backups",
    "access_key": "AKIAEXAMPLE",
    "secret_key": "wJalrXUtnFEMI/EXAMPLEKEY",
    "region": "eu-central-1",
    "prefix": "hosty",
}


async def test_s3_config_lifecycle(env):
    from tests.test_backup import FakeS3

    client, app = env["client"], env["app"]

    # nothing configured yet
    cfg = (await client.get("/api/backups/s3-config")).json()
    assert cfg["configured"] is False
    assert (await client.get("/api/backups/meta")).json()["s3_enabled"] is False

    # first save requires a secret
    resp = await client.put("/api/backups/s3-config", json={**S3_BODY, "secret_key": None})
    assert resp.status_code == 409

    # save with credentials (validation goes through the injected fake client)
    app.state.s3_client = FakeS3()
    resp = await client.put("/api/backups/s3-config", json=S3_BODY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["source"] == "db"
    assert body["has_secret"] is True
    assert body["access_key"] == "AKIAEXAMPLE"
    assert "secret" not in {k for k in body if k != "has_secret"} or "secret_key" not in body

    # the secret is encrypted at rest — never stored in plaintext
    from sqlalchemy import select

    from app.db.models import PanelSetting

    async with app.state.sessionmaker() as db:
        row = (await db.execute(select(PanelSetting))).scalar_one()
        assert "wJalrXUtnFEMI" not in row.value
        assert "secret_key_encrypted" in row.value

    # config survives without the override -> meta reads from the DB
    del app.state.s3_client
    assert (await client.get("/api/backups/meta")).json()["s3_enabled"] is True

    # blank secret on update keeps the stored one
    app.state.s3_client = FakeS3()
    resp = await client.put(
        "/api/backups/s3-config", json={**S3_BODY, "bucket": "renamed", "secret_key": ""}
    )
    assert resp.status_code == 200
    assert resp.json()["bucket"] == "renamed"

    # connection test endpoint
    assert (await client.post("/api/backups/s3-config/test")).status_code == 200

    # remove the stored config
    assert (await client.request("DELETE", "/api/backups/s3-config")).status_code == 204
    del app.state.s3_client
    assert (await client.get("/api/backups/meta")).json()["s3_enabled"] is False


async def test_schedule_rejects_empty_scope(env):
    resp = await env["client"].put(
        f"/api/sites/{env['site_id']}/backup-schedule",
        json={
            "enabled": True,
            "frequency": "daily",
            "hour": 3,
            "retention": 7,
            "include_files": False,
            "include_databases": False,
        },
    )
    assert resp.status_code == 409


async def test_backup_scope_databases_only(env):
    client, site_id = env["client"], env["site_id"]
    resp = await client.put(
        f"/api/sites/{site_id}/backup-schedule",
        json={
            "enabled": False,
            "frequency": "daily",
            "hour": 3,
            "retention": 7,
            "include_files": False,
            "include_databases": True,
            "s3_mirror": True,
        },
    )
    assert resp.status_code == 200

    resp = await client.post(f"/api/sites/{site_id}/backups")
    assert resp.status_code == 202
    op = await _poll_operation(client, resp.json()["operation_id"])
    assert op["status"] == "succeeded", op
    assert "files" not in [s["name"] for s in op["steps"]]
    assert not any(call[0] == "tar" for call in env["calls"])  # no archive was made

    backups = (await client.get(f"/api/sites/{site_id}/backups")).json()
    assert len(backups) == 1


async def test_backup_s3_mirror_opt_out(env):
    from tests.test_backup import FakeS3

    client, site_id, app = env["client"], env["site_id"], env["app"]
    fake = FakeS3()
    app.state.s3_client = fake  # S3 available...
    resp = await client.put(
        f"/api/sites/{site_id}/backup-schedule",
        json={
            "enabled": False,
            "frequency": "daily",
            "hour": 3,
            "retention": 7,
            "include_files": True,
            "include_databases": True,
            "s3_mirror": False,  # ...but this site opts out
        },
    )
    assert resp.status_code == 200
    resp = await client.post(f"/api/sites/{site_id}/backups")
    assert resp.status_code == 202
    op = await _poll_operation(client, resp.json()["operation_id"])
    assert op["status"] == "succeeded", op
    assert "s3" not in [s["name"] for s in op["steps"]]
    assert fake.objects == {}
