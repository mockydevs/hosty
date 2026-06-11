"""In-process backup scheduler (ADR-010).

A 60-second asyncio tick finds active sites whose backup is due (daily at
HH:00, or weekly anchored to Monday) and runs them serially — the global
`backup.operation_lock` already guarantees one backup at a time.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import FastAPI
from sqlalchemy import select

from app.core.clock import utcnow
from app.db.models import Operation, Site
from app.services import backup, backup_ops, s3_config
from app.services.sites import initial_steps

log = structlog.get_logger("hosty.scheduler")

TICK_SECONDS = 60


async def tick(app: FastAPI) -> int:
    """Run every due backup; returns how many were started."""
    settings = app.state.settings
    sessionmaker = app.state.sessionmaker
    now = utcnow()
    jobs: list[tuple[int, int]] = []  # (site_id, operation_id)

    async with sessionmaker() as db:
        sites = (
            (
                await db.execute(
                    select(Site).where(Site.backup_enabled.is_(True), Site.status == "active")
                )
            )
            .scalars()
            .all()
        )
        with_s3 = (
            getattr(app.state, "s3_client", None) is not None
            or await s3_config.load(db, settings) is not None
        )
        for site in sites:
            if not backup.is_due(
                now,
                frequency=site.backup_frequency,
                hour=site.backup_hour,
                last_run=site.backup_last_run_at,
            ):
                continue
            op = Operation(
                kind="backup_site",
                site_id=site.id,
                domain=site.domain,
                status="pending",
                steps_json=initial_steps(
                    backup_ops.backup_steps(
                        include_files=site.backup_include_files,
                        include_databases=site.backup_include_databases,
                        with_s3=with_s3 and site.backup_s3_mirror,
                    )
                ),
            )
            db.add(op)
            await db.commit()
            await db.refresh(op)
            jobs.append((site.id, op.id))

    for site_id, operation_id in jobs:
        try:
            await backup_ops.run_backup_site(
                sessionmaker,
                settings,
                site_id=site_id,
                operation_id=operation_id,
                s3=getattr(app.state, "s3_client", None),
            )
        except Exception as exc:  # one site failing must not stop the others
            log.error("scheduled_backup_failed", site_id=site_id, error=str(exc))

    try:
        await self_backup(app)
    except Exception as exc:  # never let self-backup break site backups
        log.error("panel_self_backup_failed", error=str(exc))
    return len(jobs)


async def loop(app: FastAPI) -> None:
    log.info("backup_scheduler_started", tick_seconds=TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await tick(app)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("scheduler_tick_failed", error=str(exc))


# --- panel self-backup (Week 23) ---------------------------------------------------

SELF_BACKUP_KEEP = 7


async def self_backup(app: FastAPI) -> str | None:
    """Snapshot the panel's own SQLite DB into `<backups_root>/_panel/` daily.

    Uses `VACUUM INTO` (consistent point-in-time copy even mid-write); prunes
    to the newest SELF_BACKUP_KEEP. Returns the new snapshot path, or None if
    today's snapshot already exists or the panel DB is not SQLite.
    """
    import os

    from sqlalchemy import text

    settings = app.state.settings
    if not settings.database_url.startswith("sqlite"):
        return None
    target_dir = os.path.join(settings.backups_root, "_panel")
    os.makedirs(target_dir, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d")
    target = os.path.join(target_dir, f"hosty-{stamp}.db")
    if os.path.exists(target):
        return None

    async with app.state.engine.connect() as conn:
        await conn.execute(text("VACUUM INTO :target").bindparams(target=target))
    log.info("panel_self_backup_written", target=target)

    snapshots = sorted(
        f for f in os.listdir(target_dir) if f.startswith("hosty-") and f.endswith(".db")
    )
    for stale in snapshots[:-SELF_BACKUP_KEEP]:
        os.unlink(os.path.join(target_dir, stale))
    return target
