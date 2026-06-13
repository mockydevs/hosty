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

    try:
        await health_sweep(app)
    except Exception as exc:  # the sweep must never break backups
        log.error("health_sweep_failed", error=str(exc))
    try:
        await stack_image_update_sweep(app)
    except Exception as exc:  # image update checks must never break backups
        log.error("stack_image_update_sweep_failed", error=str(exc))
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


# --- health & quota sweep (Phase 11c/11d) -------------------------------------------

_last_sweep_at: float | None = None
_last_stack_image_update_at: float | None = None


async def health_sweep(app: FastAPI, *, force: bool = False) -> None:
    """Hourly: emit admin notifications for down services, a nearly-full server
    disk, repeated cert-issuance failures, and clients over their disk quota
    (du-based soft limits — warnings, never hard stops)."""
    import time as _time

    global _last_sweep_at
    settings = app.state.settings
    interval = settings.usage_check_interval_seconds
    now_mono = _time.monotonic()
    if not force and _last_sweep_at is not None and now_mono - _last_sweep_at < interval:
        return
    _last_sweep_at = now_mono

    sessionmaker = app.state.sessionmaker
    from app.services import notifications, ssl, usage
    from app.system import systemd

    async with sessionmaker() as db:
        # 1. Managed services down?
        for unit in settings.managed_units:
            try:
                status = await systemd.status(unit)
            except Exception:
                continue  # systemctl unavailable (dev container) — skip quietly
            key = f"service_down:{unit}"
            if status.available and status.active_state not in ("active", "activating"):
                await notifications.emit(
                    db,
                    kind="service_down",
                    severity="error",
                    message=f"Managed service {unit} is {status.active_state}",
                    dedupe_key=key,
                    settings=settings,
                )
            else:
                await notifications.resolve(db, key)

        # 2. Server disk nearly full?
        try:
            import psutil

            percent = psutil.disk_usage("/").percent
            if percent >= settings.disk_full_threshold_percent:
                await notifications.emit(
                    db,
                    kind="disk_full",
                    severity="error",
                    message=f"Server disk is {percent:.0f}% full",
                    dedupe_key="disk_full:/",
                    settings=settings,
                )
            else:
                await notifications.resolve(db, "disk_full:/")
        except Exception as exc:
            log.warning("disk_check_failed", error=str(exc))

        # 3. Cert issuance failures (active, non-proxied sites).
        active_sites = (
            (await db.execute(select(Site).where(Site.status == "active"))).scalars().all()
        )
        for site in active_sites:
            if site.behind_cloudflare:
                continue
            try:
                probe = await ssl.probe(site.domain)
            except Exception:
                continue
            key = f"cert_failed:{site.domain}"
            if probe.status in ("no_certificate", "dns_unresolved"):
                await notifications.emit(
                    db,
                    kind="cert_failed",
                    severity="warning",
                    message=(
                        f"HTTPS certificate problem for {site.domain}: {probe.status}"
                        + (f" — {probe.detail}" if probe.detail else "")
                    ),
                    dedupe_key=key,
                    settings=settings,
                )
            else:
                await notifications.resolve(db, key)

        # 4. Per-client disk quota (soft) — Phase 11c.
        try:
            for client in await usage.all_clients_usage(db, settings):
                key = f"quota_exceeded:{client.username}"
                if (
                    client.max_disk_mb is not None
                    and client.disk_bytes > client.max_disk_mb * 1024 * 1024
                ):
                    used_mb = client.disk_bytes // (1024 * 1024)
                    await notifications.emit(
                        db,
                        kind="quota_exceeded",
                        severity="warning",
                        message=(
                            f"{client.username} is over their disk quota: "
                            f"{used_mb}MB used of {client.max_disk_mb}MB"
                        ),
                        dedupe_key=key,
                        settings=settings,
                    )
                else:
                    await notifications.resolve(db, key)
        except Exception as exc:
            log.warning("disk_quota_sweep_failed", error=str(exc))


async def stack_image_update_sweep(app: FastAPI, *, force: bool = False) -> int:
    """Periodically refresh digest locks for stable stack image tracks."""
    import time as _time

    global _last_stack_image_update_at
    settings = app.state.settings
    if not settings.stack_image_auto_update_enabled:
        return 0
    interval = settings.stack_image_update_interval_seconds
    now_mono = _time.monotonic()
    if (
        not force
        and _last_stack_image_update_at is not None
        and now_mono - _last_stack_image_update_at < interval
    ):
        return 0
    _last_stack_image_update_at = now_mono

    from app.services import stack_images

    async with app.state.sessionmaker() as db:
        changed = await stack_images.refresh_stable_image_locks(db)
    if changed and getattr(app.state, "reconciler", None) is not None:
        await app.state.reconciler.converge_all()
    return changed


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
