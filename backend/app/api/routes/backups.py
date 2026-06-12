"""Backups API (Phase 8): list, run-now, restore, delete, per-site schedule,
and UI-managed S3 credentials (secret encrypted at rest — ADR-010)."""

from __future__ import annotations

import shutil
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import fetch_owned_site, get_current_user, get_db, is_admin, require_admin
from app.core.errors import ConflictError, NotFoundError
from app.db.models import Operation, Site, User
from app.services import backup, backup_ops, s3_config
from app.services.sites import initial_steps

router = APIRouter(dependencies=[Depends(get_current_user)])  # /api/backups
site_router = APIRouter(dependencies=[Depends(get_current_user)])  # /api/sites


class BackupResponse(BaseModel):
    model_config = {"from_attributes": True}

    domain: str
    backup_id: str
    created_at: str
    size_bytes: int
    databases: list[str]
    wordpress: bool
    php_version: str
    s3: bool


class BackupsMetaResponse(BaseModel):
    s3_enabled: bool
    scheduler_enabled: bool


class OperationStartedResponse(BaseModel):
    operation_id: int


class RestoreRequest(BaseModel):
    scope: Literal["full", "files", "db"] = "full"
    confirm_domain: str


class DeleteBackupRequest(BaseModel):
    confirm_id: str


class ScheduleResponse(BaseModel):
    enabled: bool
    frequency: Literal["daily", "weekly"]
    hour: int
    retention: int
    include_files: bool
    include_databases: bool
    s3_mirror: bool
    last_run_at: str | None


class UpdateScheduleRequest(BaseModel):
    enabled: bool
    frequency: Literal["daily", "weekly"] = "daily"
    hour: int = Field(default=3, ge=0, le=23)
    retention: int = Field(default=7, ge=1, le=60)
    include_files: bool = True
    include_databases: bool = True
    s3_mirror: bool = True


class S3ConfigResponse(BaseModel):
    configured: bool
    source: Literal["db", "env"] | None
    endpoint: str
    bucket: str
    region: str
    prefix: str
    access_key: str
    has_secret: bool


class UpdateS3ConfigRequest(BaseModel):
    endpoint: str = Field(min_length=8, max_length=255)  # e.g. https://s3.amazonaws.com
    bucket: str = Field(min_length=3, max_length=63)
    access_key: str = Field(min_length=3, max_length=128)
    secret_key: str | None = Field(default=None, max_length=256)  # None/"" keeps existing
    region: str = Field(default="", max_length=32)
    prefix: str = Field(default="hosty", max_length=64)


def _settings(request: Request) -> Any:
    return request.app.state.settings


def _s3_override(request: Request) -> backup.S3Like | None:
    return getattr(request.app.state, "s3_client", None)


async def _s3_available(request: Request, db: AsyncSession) -> bool:
    if _s3_override(request) is not None:
        return True
    return await s3_config.load(db, _settings(request)) is not None


@router.get("", response_model=list[BackupResponse])
async def list_all_backups(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    backups = backup.list_backups(_settings(request).backups_root)
    if is_admin(user):
        return backups
    owned = {
        domain
        for (domain,) in (
            await db.execute(select(Site.domain).where(Site.owner_id == user.id))
        ).all()
    }
    return [b for b in backups if b.domain in owned]


@router.get("/meta", response_model=BackupsMetaResponse)
async def backups_meta(request: Request, db: AsyncSession = Depends(get_db)) -> BackupsMetaResponse:
    settings = _settings(request)
    return BackupsMetaResponse(
        s3_enabled=await _s3_available(request, db),
        scheduler_enabled=settings.backup_scheduler_enabled,
    )


# --- S3 target configuration (UI-managed, admin-only) --------------------------------

s3_admin = Depends(require_admin)


def _config_response(config: s3_config.S3Config | None) -> S3ConfigResponse:
    if config is None:
        return S3ConfigResponse(
            configured=False,
            source=None,
            endpoint="",
            bucket="",
            region="",
            prefix="hosty",
            access_key="",
            has_secret=False,
        )
    return S3ConfigResponse(
        configured=True,
        source=config.source,  # type: ignore[arg-type]
        endpoint=config.endpoint,
        bucket=config.bucket,
        region=config.region,
        prefix=config.prefix,
        access_key=config.access_key,
        has_secret=bool(config.secret_key),
    )


@router.get("/s3-config", response_model=S3ConfigResponse, dependencies=[s3_admin])
async def get_s3_config(request: Request, db: AsyncSession = Depends(get_db)) -> S3ConfigResponse:
    """Current S3 target. The secret access key is never returned."""
    return _config_response(await s3_config.load(db, _settings(request)))


async def _verify_s3(request: Request, config: s3_config.S3Config) -> None:
    """Probe the bucket with the given credentials; raise the API error if bad."""
    client = _s3_override(request) or backup.S3Client(
        endpoint=config.endpoint,
        bucket=config.bucket,
        access_key=config.access_key,
        secret_key=config.secret_key,
        region=config.region,
    )
    await client.list_keys(f"{config.prefix.strip('/')}/")


@router.put("/s3-config", response_model=S3ConfigResponse, dependencies=[s3_admin])
async def update_s3_config(
    request: Request, body: UpdateS3ConfigRequest, db: AsyncSession = Depends(get_db)
) -> S3ConfigResponse:
    """Validate the credentials against the bucket, then store them (secret
    encrypted at rest). Leaving the secret blank keeps the existing one."""
    settings = _settings(request)
    secret = body.secret_key or None
    if secret is None:
        current = await s3_config.load(db, settings)
        if current is None:
            raise ConflictError("A secret access key is required the first time")
        secret = current.secret_key
    candidate = s3_config.S3Config(
        endpoint=body.endpoint.strip().rstrip("/"),
        bucket=body.bucket.strip(),
        access_key=body.access_key.strip(),
        secret_key=secret,
        region=body.region.strip(),
        prefix=body.prefix.strip().strip("/") or "hosty",
    )
    await _verify_s3(request, candidate)  # raises backup_error on bad credentials
    saved = await s3_config.save(
        db,
        settings,
        endpoint=candidate.endpoint,
        bucket=candidate.bucket,
        access_key=candidate.access_key,
        secret_key=candidate.secret_key,
        region=candidate.region,
        prefix=candidate.prefix,
    )
    return _config_response(saved)


@router.post("/s3-config/test", response_model=BackupsMetaResponse, dependencies=[s3_admin])
async def test_s3_config(
    request: Request, db: AsyncSession = Depends(get_db)
) -> BackupsMetaResponse:
    """Probe the stored configuration; 502 with details when unreachable."""
    settings = _settings(request)
    config = await s3_config.load(db, settings)
    if config is None:
        raise NotFoundError("No S3 configuration stored yet")
    await _verify_s3(request, config)
    return BackupsMetaResponse(s3_enabled=True, scheduler_enabled=settings.backup_scheduler_enabled)


@router.delete("/s3-config", status_code=status.HTTP_204_NO_CONTENT, dependencies=[s3_admin])
async def delete_s3_config(db: AsyncSession = Depends(get_db)) -> None:
    if not await s3_config.clear(db):
        raise NotFoundError("No S3 configuration stored")


# --- per-site backups ----------------------------------------------------------------


@site_router.get("/{site_id}/backups", response_model=list[BackupResponse])
async def list_site_backups(
    request: Request,
    site_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    site = await fetch_owned_site(db, user, site_id)
    return backup.list_backups(_settings(request).backups_root, site.domain)


@site_router.post(
    "/{site_id}/backups",
    response_model=OperationStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_backup(
    request: Request,
    site_id: int,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OperationStartedResponse:
    site = await fetch_owned_site(db, user, site_id)
    if site.status != "active":
        raise ConflictError(f"Site is {site.status}; only active sites can be backed up")
    with_s3 = await _s3_available(request, db) and site.backup_s3_mirror
    op = Operation(
        kind="backup_site",
        site_id=site.id,
        domain=site.domain,
        status="pending",
        steps_json=initial_steps(
            backup_ops.backup_steps(
                include_files=site.backup_include_files,
                include_databases=site.backup_include_databases,
                with_s3=with_s3,
            )
        ),
    )
    db.add(op)
    await db.commit()
    await db.refresh(op)
    background.add_task(
        backup_ops.run_backup_site,
        request.app.state.sessionmaker,
        _settings(request),
        site_id=site.id,
        operation_id=op.id,
        s3=_s3_override(request),
    )
    return OperationStartedResponse(operation_id=op.id)


@site_router.post(
    "/{site_id}/backups/{backup_id}/restore",
    response_model=OperationStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_restore(
    request: Request,
    site_id: int,
    backup_id: str,
    body: RestoreRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OperationStartedResponse:
    site = await fetch_owned_site(db, user, site_id)
    backup.validate_backup_id(backup_id)
    if body.confirm_domain.strip().lower() != site.domain:
        raise ConflictError("Confirmation does not match the site domain")
    if site.status != "active":
        raise ConflictError(f"Site is {site.status}; only active sites can be restored")
    # The backup must exist locally or in S3.
    local = backup.read_manifest(
        backup.backup_dir(_settings(request).backups_root, site.domain, backup_id)
    )
    if local is None and not await _s3_available(request, db):
        raise NotFoundError(f"Backup {backup_id} not found for {site.domain}")
    op = Operation(
        kind="restore_site",
        site_id=site.id,
        domain=site.domain,
        status="pending",
        steps_json=initial_steps(backup_ops.RESTORE_STEPS),
    )
    db.add(op)
    await db.commit()
    await db.refresh(op)
    background.add_task(
        backup_ops.run_restore_site,
        request.app.state.sessionmaker,
        _settings(request),
        site_id=site.id,
        operation_id=op.id,
        backup_id=backup_id,
        scope=body.scope,
        s3=_s3_override(request),
    )
    return OperationStartedResponse(operation_id=op.id)


@site_router.delete("/{site_id}/backups/{backup_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backup(
    request: Request,
    site_id: int,
    backup_id: str,
    body: DeleteBackupRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    site = await fetch_owned_site(db, user, site_id)
    backup.validate_backup_id(backup_id)
    if body.confirm_id.strip() != backup_id:
        raise ConflictError("Confirmation does not match the backup id")
    directory = backup.backup_dir(_settings(request).backups_root, site.domain, backup_id)
    if not directory.is_dir():
        raise NotFoundError(f"Backup {backup_id} not found for {site.domain}")
    shutil.rmtree(directory)


def _schedule_response(site: Site) -> ScheduleResponse:
    return ScheduleResponse(
        enabled=site.backup_enabled,
        frequency=site.backup_frequency,  # type: ignore[arg-type]
        hour=site.backup_hour,
        retention=site.backup_retention,
        include_files=site.backup_include_files,
        include_databases=site.backup_include_databases,
        s3_mirror=site.backup_s3_mirror,
        last_run_at=site.backup_last_run_at.isoformat() + "Z" if site.backup_last_run_at else None,
    )


@site_router.get("/{site_id}/backup-schedule", response_model=ScheduleResponse)
async def get_schedule(
    site_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScheduleResponse:
    return _schedule_response(await fetch_owned_site(db, user, site_id))


@site_router.put("/{site_id}/backup-schedule", response_model=ScheduleResponse)
async def update_schedule(
    site_id: int,
    body: UpdateScheduleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScheduleResponse:
    if not body.include_files and not body.include_databases:
        raise ConflictError("A backup must include files, databases, or both")
    site = await fetch_owned_site(db, user, site_id)
    site.backup_enabled = body.enabled
    site.backup_frequency = body.frequency
    site.backup_hour = body.hour
    site.backup_retention = body.retention
    site.backup_include_files = body.include_files
    site.backup_include_databases = body.include_databases
    site.backup_s3_mirror = body.s3_mirror
    await db.commit()
    return _schedule_response(site)
