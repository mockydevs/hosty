"""Backup and restore as tracked Operations (reuses the sites pipeline).

Failure semantics:
- During backup, failures before `finalize` roll back the staging directory;
  after `finalize` the local backup is durable. An S3 mirror failure marks the
  operation failed but NEVER deletes the completed local backup.
- Restore has no compensation: it overwrites in place by design. The `verify`
  step (checksums) runs before anything is touched.

What each backup contains is per-site configuration (files / databases /
S3 mirror), chosen in the UI and honored by both run-now and the scheduler.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import utcnow
from app.core.config import Settings
from app.db.models import Database, Operation, Site
from app.services import backup, s3_config
from app.services.sites import _finish, _run_pipeline, _Step, site_dir_for
from app.system import fs

log = structlog.get_logger("hosty.backup_ops")


def backup_steps(
    *, include_files: bool, include_databases: bool, with_s3: bool
) -> list[tuple[str, str]]:
    steps = [("prepare", "Prepare staging directory")]
    if include_databases:
        steps.append(("databases", "Dump databases"))
    if include_files:
        steps.append(("files", "Archive site files"))
    steps.append(("finalize", "Write manifest and apply retention"))
    if with_s3:
        steps.append(("s3", "Mirror to S3"))
    return steps


RESTORE_STEPS = [
    ("fetch", "Fetch backup (from S3 if not local)"),
    ("verify", "Verify checksums"),
    ("files", "Restore site files"),
    ("databases", "Restore databases"),
]


async def resolve_s3(
    db: AsyncSession, settings: Settings, override: backup.S3Like | None
) -> tuple[backup.S3Like | None, str]:
    """The S3 client (injected, DB-configured, or env fallback) and key prefix."""
    config = await s3_config.load(db, settings)
    prefix = config.prefix if config is not None else settings.s3_prefix
    if override is not None:
        return override, prefix
    if config is None:
        return None, prefix
    return (
        backup.S3Client(
            endpoint=config.endpoint,
            bucket=config.bucket,
            access_key=config.access_key,
            secret_key=config.secret_key,
            region=config.region,
        ),
        prefix,
    )


async def run_backup_site(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    site_id: int,
    operation_id: int,
    s3: backup.S3Like | None = None,
) -> None:
    async with backup.operation_lock, sessionmaker() as db:
        site = await db.get(Site, site_id)
        op = await db.get(Operation, operation_id)
        assert site is not None and op is not None
        op.status = "running"
        site.backup_last_run_at = utcnow()  # set up-front: a failed run must not retry-storm
        await db.commit()

        db_names = (
            [
                row.name
                for row in (
                    (await db.execute(select(Database).where(Database.site_id == site.id)))
                    .scalars()
                    .all()
                )
            ]
            if site.backup_include_databases
            else []
        )
        s3_client, prefix = await resolve_s3(db, settings, s3)
        mirror = s3_client is not None and site.backup_s3_mirror
        backup_id = backup.new_backup_id()
        domain_dir = Path(settings.backups_root) / site.domain
        staging = domain_dir / f".staging-{backup_id}"
        final = domain_dir / backup_id

        async def do_prepare() -> None:
            staging.mkdir(parents=True, exist_ok=True)

        async def undo_prepare() -> None:
            shutil.rmtree(staging, ignore_errors=True)

        async def do_databases() -> None:
            await backup.dump_databases(db_names, staging)

        async def do_files() -> None:
            await backup.archive_files(site_dir_for(site.domain, settings), staging)

        async def do_finalize() -> None:
            backup.write_manifest(
                staging,
                backup_id=backup_id,
                domain=site.domain,
                php_version=site.php_version,
                wordpress=site.wordpress,
                databases=db_names,
            )
            staging.rename(final)
            backup.prune(settings.backups_root, site.domain, site.backup_retention)

        async def do_s3() -> None:
            assert s3_client is not None
            try:
                await backup.upload_backup(s3_client, prefix, final, site.domain, backup_id)
            except backup.BackupError as exc:
                # The local backup is durable; surface a precise message.
                raise backup.BackupError(
                    f"local backup {backup_id} created, but the S3 mirror failed: {exc}"
                ) from exc

        steps = [_Step("prepare", do_prepare, undo_prepare)]
        if site.backup_include_databases:
            steps.append(_Step("databases", do_databases, None))
        if site.backup_include_files:
            steps.append(_Step("files", do_files, None))
        steps.append(_Step("finalize", do_finalize, None))  # durable from here on
        if mirror:
            steps.append(_Step("s3", do_s3, None))

        ok, error = await _run_pipeline(db, op, steps)
        await _finish(db, op, status="succeeded" if ok else "failed", error=error)
        log.info("backup_finished", domain=site.domain, backup_id=backup_id, ok=ok)
        if not ok:
            # Week 21 / Phase 11d: surface the failure on the dashboard.
            from app.services import notifications

            try:
                await notifications.emit(
                    db,
                    kind="backup_failed",
                    severity="error",
                    message=f"Backup of {site.domain} failed: {error}",
                    dedupe_key=f"backup_failed:{site.domain}",
                )
            except Exception as exc:  # notifying must never break the operation
                log.warning("backup_failure_notification_failed", error=str(exc))
        else:
            import contextlib

            from app.services import notifications

            with contextlib.suppress(Exception):  # resolving must never break the operation
                await notifications.resolve(db, f"backup_failed:{site.domain}")


async def run_restore_site(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    site_id: int,
    operation_id: int,
    backup_id: str,
    scope: str,  # full | files | db
    s3: backup.S3Like | None = None,
) -> None:
    async with backup.operation_lock, sessionmaker() as db:
        site = await db.get(Site, site_id)
        op = await db.get(Operation, operation_id)
        assert site is not None and op is not None
        op.status = "running"
        await db.commit()

        s3_client, prefix = await resolve_s3(db, settings, s3)
        directory = backup.backup_dir(settings.backups_root, site.domain, backup_id)
        site_dir = site_dir_for(site.domain, settings)
        manifest_holder: dict = {}

        async def do_fetch() -> None:
            if backup.read_manifest(directory) is not None:
                return
            if s3_client is None:
                raise backup.BackupError(f"Backup {backup_id} is not available locally")
            await backup.download_backup(
                s3_client, prefix, settings.backups_root, site.domain, backup_id
            )

        async def do_verify() -> None:
            manifest = backup.read_manifest(directory)
            if manifest is None:
                raise backup.BackupError(f"Backup {backup_id} has no readable manifest")
            backup.verify_checksums(directory, manifest)
            manifest_holder.update(manifest)

        def _has_archive() -> bool:
            return any(
                f.get("path") == backup.FILES_ARCHIVE for f in manifest_holder.get("files", [])
            )

        async def do_files() -> None:
            if scope not in ("full", "files") or not _has_archive():
                return  # backup was made without files — nothing to restore
            await backup.restore_files(directory, site_dir)
            await fs.chown_recursive(site.site_user, site_dir, root=settings.sites_root)

        async def do_databases() -> None:
            if scope not in ("full", "db"):
                return
            await backup.restore_databases(directory, list(manifest_holder.get("databases", [])))

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("fetch", do_fetch, None),
                _Step("verify", do_verify, None),
                _Step("files", do_files, None),
                _Step("databases", do_databases, None),
            ],
        )
        await _finish(db, op, status="succeeded" if ok else "failed", error=error)
        log.info("restore_finished", domain=site.domain, backup_id=backup_id, scope=scope, ok=ok)
