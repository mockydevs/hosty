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

    async def fake_chown(user, path, *, root):  # restore touches ownership
        calls.append(["chown", user, path])

    monkeypatch.setattr("app.services.backup_ops.fs.chown_recursive", fake_chown)

    s = settings.model_copy(update={"backups_root": str(tmp_path / "backups")})
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

    # restore: wrong confirmation -> 409; right -> runs tar -xf + chown
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
    assert any(call[:3] == ["tar", "--zstd", "-xf"] for call in env["calls"])
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
    s_cfg = s.model_copy(update={"s3_bucket": "b"})  # prefix defaults to "hosty"
    await backup.upload_backup(s3, s_cfg, source, "example.com", "20260611T030000Z")

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
