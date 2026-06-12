"""Week 21: GET /api/operations — the dashboard's backup-failure feed."""

from __future__ import annotations

from datetime import timedelta

from app.core.clock import utcnow
from app.db.models import Operation


async def _seed(app) -> None:
    async with app.state.sessionmaker() as db:
        db.add_all(
            [
                Operation(
                    kind="backup_site",
                    domain="old-fail.example",
                    status="failed",
                    error="disk full",
                    created_at=utcnow() - timedelta(days=30),
                ),
                Operation(
                    kind="backup_site",
                    domain="fresh-fail.example",
                    status="failed",
                    error="mysqldump exited 2",
                ),
                Operation(kind="backup_site", domain="ok.example", status="succeeded"),
                Operation(
                    kind="restore_site",
                    domain="restore-fail.example",
                    status="failed",
                    error="checksum mismatch",
                ),
                Operation(kind="create_site", domain="other.example", status="failed"),
            ]
        )
        await db.commit()


async def test_list_operations_requires_auth(client):
    assert (await client.get("/api/operations")).status_code == 401


async def test_list_operations_newest_first(admin_client, app):
    await _seed(app)
    ops = (await admin_client.get("/api/operations")).json()
    assert len(ops) == 5
    assert [o["id"] for o in ops] == sorted((o["id"] for o in ops), reverse=True)
    assert "steps" not in ops[0]  # summaries are lightweight


async def test_filter_backup_failures_for_dashboard(admin_client, app):
    await _seed(app)
    resp = await admin_client.get(
        "/api/operations",
        params=[
            ("kind", "backup_site"),
            ("kind", "restore_site"),
            ("status", "failed"),
            ("since_hours", 168),
            ("limit", 10),
        ],
    )
    assert resp.status_code == 200
    ops = resp.json()
    domains = {o["domain"] for o in ops}
    # Succeeded runs, other kinds, and runs older than the window are excluded.
    assert domains == {"fresh-fail.example", "restore-fail.example"}
    errors = {o["error"] for o in ops}
    assert errors == {"mysqldump exited 2", "checksum mismatch"}


async def test_limit_is_applied(admin_client, app):
    await _seed(app)
    ops = (await admin_client.get("/api/operations", params={"limit": 2})).json()
    assert len(ops) == 2
