"""Sites API: list/create/delete sites, per-domain SSL status.

Provisioning is asynchronous: POST/DELETE return 202 with an operation id the
UI polls via /api/operations/{id}.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import fetch_owned_site, get_current_user, get_db, is_admin
from app.core.clock import utcnow
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import Operation, Site, User
from app.services import quotas
from app.services import sites as sites_service
from app.services import ssl as ssl_service
from app.services import wordpress as wordpress_service
from app.services.sites import (
    DELETE_STEPS,
    DomainValidationError,
    derive_site_user,
    doc_root_for,
    initial_steps,
    validate_domain,
)

router = APIRouter(dependencies=[Depends(get_current_user)])


class DomainError(AppError):
    status_code = 422
    code = "invalid_domain"


class SiteOperationError(AppError):
    status_code = 502
    code = "site_operation_failed"


class SiteResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    domain: str
    site_user: str
    doc_root: str
    php_version: str
    status: str
    error_message: str | None
    php_memory_limit: str
    php_upload_max_filesize: str
    wordpress: bool
    behind_cloudflare: bool
    staging_of: int | None = None
    created_at: datetime


class CreateSiteRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    php_version: str | None = None
    # Week 17: also create a PowerDNS zone (SOA/NS + A -> server IP) for the domain.
    create_dns_zone: bool = False

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, v: str) -> str:
        try:
            return validate_domain(v)
        except DomainValidationError as exc:
            raise ValueError(str(exc)) from exc


class DeleteSiteRequest(BaseModel):
    confirm_domain: str


class OperationAccepted(BaseModel):
    site: SiteResponse
    operation_id: int


class CertStatusResponse(BaseModel):
    domain: str
    status: str
    issuer: str | None
    not_after: datetime | None
    detail: str | None


def _settings(request: Request):
    return request.app.state.settings


@router.get("", response_model=list[SiteResponse])
async def list_sites(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> Any:
    query = select(Site).order_by(Site.domain)
    if not is_admin(user):
        query = query.where(Site.owner_id == user.id)
    return (await db.execute(query)).scalars().all()


@router.get("/{site_id}", response_model=SiteResponse)
async def get_site(
    site_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    return await fetch_owned_site(db, user, site_id)


@router.post("", response_model=OperationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_site(
    request: Request,
    body: CreateSiteRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    settings = _settings(request)
    php_version = body.php_version or settings.default_php_version
    if php_version not in settings.php_versions:
        raise DomainError(f"Unsupported PHP version: {php_version}")
    create_dns_zone = body.create_dns_zone
    if create_dns_zone and not settings.dns_enabled:
        raise ConflictError("DNS management is disabled on this server")
    # Phase 11b: clients may auto-create zones too — the pipeline records
    # ownership in dns_zone_owners, so the zone is theirs to manage.

    limits = await quotas.effective_limits(db, user)
    if not is_admin(user) and limits.max_sites is not None:
        owned = (
            await db.execute(select(func.count()).select_from(Site).where(Site.owner_id == user.id))
        ).scalar_one()
        if owned >= limits.max_sites:
            raise ConflictError(f"Site quota reached ({limits.max_sites})")

    existing = (
        await db.execute(select(Site).where(Site.domain == body.domain))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A site for {body.domain} already exists")

    site = Site(
        owner_id=user.id,
        domain=body.domain,
        site_user=derive_site_user(body.domain),
        doc_root=doc_root_for(body.domain, settings),
        php_version=php_version,
        status="provisioning",
    )
    db.add(site)
    await db.flush()
    op = Operation(
        kind="create_site",
        site_id=site.id,
        domain=site.domain,
        steps_json=initial_steps(sites_service.create_steps(with_dns_zone=create_dns_zone)),
    )
    db.add(op)
    await db.commit()
    await db.refresh(site)
    await db.refresh(op)

    background.add_task(
        sites_service.run_create_site,
        request.app.state.sessionmaker,
        settings,
        site_id=site.id,
        operation_id=op.id,
        create_dns_zone=create_dns_zone,
        pdns_client=getattr(request.app.state, "pdns_client", None),
    )
    return OperationAccepted(site=SiteResponse.model_validate(site), operation_id=op.id)


@router.delete("/{site_id}", response_model=OperationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def delete_site(
    request: Request,
    site_id: int,
    body: DeleteSiteRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    site = await fetch_owned_site(db, user, site_id)
    if body.confirm_domain.strip().lower() != site.domain:
        raise ConflictError("Confirmation does not match the site domain")
    if site.status == "deleting":
        raise ConflictError("Site is already being deleted")

    site.status = "deleting"
    op = Operation(
        kind="delete_site",
        site_id=site.id,
        domain=site.domain,
        steps_json=initial_steps(DELETE_STEPS),
    )
    db.add(op)
    await db.commit()
    await db.refresh(site)
    await db.refresh(op)

    background.add_task(
        sites_service.run_delete_site,
        request.app.state.sessionmaker,
        _settings(request),
        site_id=site.id,
        operation_id=op.id,
    )
    return OperationAccepted(site=SiteResponse.model_validate(site), operation_id=op.id)


@router.get("/{site_id}/ssl", response_model=CertStatusResponse)
async def site_ssl_status(
    site_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    site = await fetch_owned_site(db, user, site_id)
    result = await ssl_service.probe(site.domain, verify=not site.behind_cloudflare)
    return CertStatusResponse(
        domain=result.domain,
        status=result.status,
        issuer=result.issuer,
        not_after=result.not_after,
        detail=result.detail,
    )


@router.post("/{site_id}/ssl/renew", response_model=CertStatusResponse)
async def renew_site_ssl(
    request: Request,
    site_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Re-apply the Caddy config so it (re)attempts certificate issuance.

    Caddy renews valid certificates on its own; this endpoint covers the
    retry cases (initial issuance failed, DNS fixed after creation) and
    returns a fresh probe of the resulting certificate status.
    """
    site = await _get_active_site(db, user, site_id)
    try:
        await sites_service.resync_caddy(db, _settings(request))
    except Exception as exc:
        raise SiteOperationError(f"Certificate renewal failed: {exc}") from exc
    result = await ssl_service.probe(site.domain, verify=not site.behind_cloudflare)
    return CertStatusResponse(
        domain=result.domain,
        status=result.status,
        issuer=result.issuer,
        not_after=result.not_after,
        detail=result.detail,
    )


class CloudflareProxyRequest(BaseModel):
    behind_cloudflare: bool


@router.patch("/{site_id}/cloudflare-proxy", response_model=SiteResponse)
async def set_cloudflare_proxy(
    request: Request,
    site_id: int,
    body: CloudflareProxyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Toggle 'behind the Cloudflare proxy' for a site.

    When enabled, Caddy issues an internal origin certificate for the domain
    instead of attempting ACME HTTP-01 (which the proxy would break); Cloudflare
    terminates public TLS at the edge (SSL mode "Full").
    """
    site = await _get_active_site(db, user, site_id)
    if site.behind_cloudflare == body.behind_cloudflare:
        return site
    previous = site.behind_cloudflare
    site.behind_cloudflare = body.behind_cloudflare
    try:
        await sites_service.resync_caddy(db, _settings(request))
    except Exception as exc:
        site.behind_cloudflare = previous
        await db.commit()
        raise SiteOperationError(f"Could not update the web server: {exc}") from exc
    await db.commit()
    await db.refresh(site)
    return site


# --- operations ----------------------------------------------------------------

operations_router = APIRouter(dependencies=[Depends(get_current_user)])


class OperationStep(BaseModel):
    name: str
    label: str
    status: str


class OperationResponse(BaseModel):
    id: int
    kind: str
    site_id: int | None
    domain: str
    status: str
    steps: list[OperationStep]
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class OperationSummaryResponse(BaseModel):
    """Listing entry (Week 21 dashboard notifications): no step detail."""

    id: int
    kind: str
    site_id: int | None
    domain: str
    status: str
    error: str | None
    created_at: datetime
    finished_at: datetime | None


async def _operation_visible(db: AsyncSession, user: User, op: Operation) -> bool:
    """Clients only see operations on their own sites (404 otherwise)."""
    if is_admin(user):
        return True
    if op.site_id is None:
        return False
    site = await db.get(Site, op.site_id)
    return site is not None and site.owner_id == user.id


@operations_router.get("", response_model=list[OperationSummaryResponse])
async def list_operations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    kind: list[str] = Query(default=[]),
    status: str | None = Query(default=None),
    since_hours: int | None = Query(default=None, ge=1, le=24 * 90),
    limit: int = Query(default=20, ge=1, le=100),
) -> Any:
    """Recent operations, newest first. Used by the dashboard to surface
    backup failures (kind=backup_site&status=failed&since_hours=168)."""
    query = select(Operation).order_by(Operation.id.desc()).limit(limit)
    if kind:
        query = query.where(Operation.kind.in_(kind))
    if status is not None:
        query = query.where(Operation.status == status)
    if since_hours is not None:
        query = query.where(Operation.created_at >= utcnow() - timedelta(hours=since_hours))
    if not is_admin(user):
        query = query.join(Site, Site.id == Operation.site_id).where(Site.owner_id == user.id)
    rows = (await db.execute(query)).scalars().all()
    return [OperationSummaryResponse.model_validate(op, from_attributes=True) for op in rows]


@operations_router.get("/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    op = await db.get(Operation, operation_id)
    if op is None or not await _operation_visible(db, user, op):
        raise NotFoundError("Operation not found")
    return OperationResponse(
        id=op.id,
        kind=op.kind,
        site_id=op.site_id,
        domain=op.domain,
        status=op.status,
        steps=[OperationStep(**s) for s in json.loads(op.steps_json)],
        error=op.error,
        created_at=op.created_at,
        finished_at=op.finished_at,
    )


# --- PHP management (Week 11) ----------------------------------------------------


class ChangePhpVersionRequest(BaseModel):
    php_version: str


class PhpSettingsRequest(BaseModel):
    memory_limit: str = Field(pattern=r"^[1-9][0-9]{0,3}M$")
    upload_max_filesize: str = Field(pattern=r"^[1-9][0-9]{0,3}M$")


async def _get_active_site(db: AsyncSession, user: User, site_id: int) -> Site:
    site = await fetch_owned_site(db, user, site_id)
    if site.status != "active":
        raise ConflictError(f"Site is {site.status}; wait until it is active")
    return site


@router.post("/{site_id}/php", response_model=SiteResponse)
async def change_php_version(
    request: Request,
    site_id: int,
    body: ChangePhpVersionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    settings = _settings(request)
    if body.php_version not in settings.php_versions:
        raise DomainError(f"Unsupported PHP version: {body.php_version}")
    site = await _get_active_site(db, user, site_id)
    try:
        await sites_service.change_php_version(db, settings, site, body.php_version)
    except Exception as exc:
        # The service already reverted to the old pool; report what happened.
        raise SiteOperationError(f"PHP version switch failed: {exc}") from exc
    await db.refresh(site)
    return site


@router.patch("/{site_id}/php-settings", response_model=SiteResponse)
async def update_php_settings(
    request: Request,
    site_id: int,
    body: PhpSettingsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    site = await _get_active_site(db, user, site_id)
    await sites_service.update_php_settings(
        _settings(request),
        site,
        db,
        memory_limit=body.memory_limit,
        upload_max_filesize=body.upload_max_filesize,
    )
    await db.refresh(site)
    return site


# --- WordPress (Weeks 12-13) ------------------------------------------------------


class WpInstallRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    admin_user: str = Field(min_length=3, max_length=60, pattern=r"^[a-zA-Z0-9_.\-@ ]+$")
    admin_password: str = Field(min_length=12, max_length=128)
    admin_email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    locale: str = "en_US"
    version: str = "latest"

    @field_validator("locale")
    @classmethod
    def _locale(cls, v: str) -> str:
        return wordpress_service.validate_locale(v)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        return wordpress_service.validate_wp_version(v)


class WpStatusResponse(BaseModel):
    installed: bool
    healthy: bool = True
    detail: str | None = None
    version: str | None
    update_available: str | None
    plugin_count: int | None
    theme_count: int | None


class WpInstallAccepted(BaseModel):
    operation_id: int


class WpActionResponse(BaseModel):
    action: str
    url: str | None = None


@router.get("/{site_id}/wordpress", response_model=WpStatusResponse)
async def wordpress_status(
    site_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    site = await fetch_owned_site(db, user, site_id)
    result = await wordpress_service.status(site)
    return WpStatusResponse(
        installed=result.installed,
        healthy=result.healthy,
        detail=result.detail,
        version=result.version,
        update_available=result.update_available,
        plugin_count=result.plugin_count,
        theme_count=result.theme_count,
    )


@router.post(
    "/{site_id}/wordpress",
    response_model=WpInstallAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def install_wordpress(
    request: Request,
    site_id: int,
    body: WpInstallRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    site = await _get_active_site(db, user, site_id)
    if site.wordpress or await wordpress_service.is_installed(site):
        raise ConflictError("WordPress is already installed on this site")

    op = Operation(
        kind="install_wordpress",
        site_id=site.id,
        domain=site.domain,
        steps_json=initial_steps(wordpress_service.INSTALL_STEPS),
    )
    db.add(op)
    await db.commit()
    await db.refresh(op)

    background.add_task(
        wordpress_service.run_install_wordpress,
        request.app.state.sessionmaker,
        _settings(request),
        site_id=site.id,
        operation_id=op.id,
        params=wordpress_service.InstallParams(
            title=body.title,
            admin_user=body.admin_user,
            admin_password=body.admin_password,
            admin_email=body.admin_email,
            locale=body.locale,
            version=body.version,
        ),
    )
    return WpInstallAccepted(operation_id=op.id)


@router.post("/{site_id}/wordpress/actions/{action}", response_model=WpActionResponse)
async def wordpress_action(
    site_id: int,
    action: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    site = await _get_active_site(db, user, site_id)
    if not site.wordpress:
        raise ConflictError("WordPress is not installed on this site")
    if action not in wordpress_service.WP_ACTIONS:
        raise NotFoundError(f"Unknown action: {action}")
    url = await wordpress_service.run_action(site, action)
    return WpActionResponse(action=action, url=url)


# --- PHP error log viewer (Phase 11d) -------------------------------------------------


class PhpLogResponse(BaseModel):
    path: str
    exists: bool
    lines: list[str]


@router.get("/{site_id}/logs/php", response_model=PhpLogResponse)
async def php_error_log(
    site_id: int,
    lines: int = 200,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PhpLogResponse:
    """Tail the site's PHP error log (pool config: /home/<user>/php-error.log).
    Owner-scoped: clients see only their own sites' logs."""
    site = await fetch_owned_site(db, user, site_id)
    log_path = f"/home/{site.site_user}/php-error.log"
    count = min(max(lines, 1), 1000)
    try:
        with open(log_path, "rb") as fh:
            # Tail without reading the whole file: read at most ~512KB from the end.
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return PhpLogResponse(path=log_path, exists=False, lines=[])
    except OSError as exc:
        raise SiteOperationError(f"Cannot read PHP error log: {exc}") from exc
    return PhpLogResponse(path=log_path, exists=True, lines=tail[-count:])


# --- site import (Phase 11d) ----------------------------------------------------------


class ImportUploadResponse(BaseModel):
    upload_id: str
    size_bytes: int


class StartImportRequest(BaseModel):
    files_upload_id: str | None = None
    sql_upload_id: str | None = None
    # Required with sql_upload_id: which panel-managed DB receives the dump.
    target_db: str | None = None
    # WordPress: rewrite this old domain to the site's domain after import.
    old_domain: str | None = Field(default=None, max_length=253)


UPLOAD_ID_RE = r"^[a-f0-9]{32}\.(files\.(tar\.gz|tgz|zip)|sql)$"


def _upload_path(settings, upload_id: str) -> str:
    import re as _re

    if not _re.fullmatch(UPLOAD_ID_RE, upload_id):
        raise NotFoundError("Upload not found")
    return f"{settings.uploads_dir}/{upload_id}"


@router.post(
    "/{site_id}/import/upload",
    response_model=ImportUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_import_file(
    request: Request,
    site_id: int,
    kind: str,
    filename: str = "",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportUploadResponse:
    """Raw-body upload (no multipart): ?kind=files&filename=site.tar.gz or ?kind=sql.
    The body is streamed to disk, never held in memory."""
    import os as _os
    import uuid as _uuid

    await fetch_owned_site(db, user, site_id)
    settings = _settings(request)
    if kind == "files":
        lowered = filename.lower()
        if lowered.endswith(".tar.gz"):
            suffix = "files.tar.gz"
        elif lowered.endswith(".tgz"):
            suffix = "files.tgz"
        elif lowered.endswith(".zip"):
            suffix = "files.zip"
        else:
            raise ConflictError("Files archive must be .tar.gz, .tgz or .zip")
    elif kind == "sql":
        suffix = "sql"
    else:
        raise NotFoundError("kind must be 'files' or 'sql'")
    upload_id = f"{_uuid.uuid4().hex}.{suffix}"
    _os.makedirs(settings.uploads_dir, exist_ok=True)
    path = f"{settings.uploads_dir}/{upload_id}"
    size = 0
    with open(path, "wb") as fh:
        async for chunk in request.stream():
            size += len(chunk)
            fh.write(chunk)
    if size == 0:
        _os.unlink(path)
        raise ConflictError("Upload was empty")
    return ImportUploadResponse(upload_id=upload_id, size_bytes=size)


@router.post(
    "/{site_id}/import", response_model=OperationAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def start_import(
    request: Request,
    site_id: int,
    body: StartImportRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Import uploaded files/SQL into the site. Overwrites in place — take a
    backup first (the UI insists)."""
    import os as _os

    from app.services import site_import as import_service

    site = await _get_active_site(db, user, site_id)
    settings = _settings(request)
    if not body.files_upload_id and not body.sql_upload_id:
        raise ConflictError("Provide a files upload, an SQL upload, or both")
    archive_path = sql_path = None
    if body.files_upload_id:
        archive_path = _upload_path(settings, body.files_upload_id)
        if not _os.path.exists(archive_path):
            raise NotFoundError("Files upload not found — upload it first")
    if body.sql_upload_id:
        sql_path = _upload_path(settings, body.sql_upload_id)
        if not _os.path.exists(sql_path):
            raise NotFoundError("SQL upload not found — upload it first")
        if not body.target_db:
            raise ConflictError("target_db is required when importing an SQL dump")
    old_domain = None
    if body.old_domain:
        old_domain = validate_domain(body.old_domain)
        if old_domain == site.domain:
            old_domain = None  # nothing to rewrite

    op = Operation(
        kind="import_site",
        site_id=site.id,
        domain=site.domain,
        steps_json=initial_steps(
            import_service.import_steps(
                with_files=archive_path is not None,
                with_sql=sql_path is not None,
                with_replace=old_domain is not None and site.wordpress,
            )
        ),
    )
    db.add(op)
    await db.commit()
    await db.refresh(op)

    background.add_task(
        import_service.run_import_site,
        request.app.state.sessionmaker,
        settings,
        site_id=site.id,
        operation_id=op.id,
        archive_path=archive_path,
        sql_path=sql_path,
        old_domain=old_domain,
        target_db=body.target_db,
    )
    return OperationAccepted(site=SiteResponse.model_validate(site), operation_id=op.id)


# --- staging clones (Phase 11d) -------------------------------------------------------


class PushStagingRequest(BaseModel):
    confirm_domain: str


@router.post(
    "/{site_id}/staging", response_model=OperationAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def create_staging(
    request: Request,
    site_id: int,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Clone this site (files + DB) to staging.<domain>."""
    from app.services import staging as staging_service

    site = await _get_active_site(db, user, site_id)
    if site.staging_of is not None:
        raise ConflictError("This already is a staging site")
    staging_domain = staging_service.staging_domain_for(site.domain)
    existing = (
        await db.execute(select(Site).where(Site.domain == staging_domain))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A staging site for {site.domain} already exists")

    staging_site = staging_service.make_staging_site(site, _settings(request))
    db.add(staging_site)
    await db.flush()
    op = Operation(
        kind="create_staging",
        site_id=staging_site.id,
        domain=staging_domain,
        steps_json=initial_steps(staging_service.CREATE_STAGING_STEPS),
    )
    db.add(op)
    await db.commit()
    await db.refresh(op)
    await db.refresh(staging_site)

    background.add_task(
        staging_service.run_create_staging,
        request.app.state.sessionmaker,
        _settings(request),
        source_site_id=site.id,
        staging_site_id=staging_site.id,
        operation_id=op.id,
    )
    return OperationAccepted(site=SiteResponse.model_validate(staging_site), operation_id=op.id)


@router.post(
    "/{site_id}/staging/push",
    response_model=OperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def push_staging(
    request: Request,
    site_id: int,
    body: PushStagingRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Push a staging clone back to production (files with --delete + DB replay).
    Destructive: requires typing the PRODUCTION domain to confirm."""
    from app.services import staging as staging_service

    staging_site = await _get_active_site(db, user, site_id)
    if staging_site.staging_of is None:
        raise ConflictError("This site is not a staging clone")
    production = await db.get(Site, staging_site.staging_of)
    if production is None:
        raise ConflictError("The production site for this clone no longer exists")
    if body.confirm_domain.strip().lower() != production.domain:
        raise ConflictError("Confirmation does not match the production domain")

    op = Operation(
        kind="push_staging",
        site_id=production.id,
        domain=production.domain,
        steps_json=initial_steps(staging_service.PUSH_STAGING_STEPS),
    )
    db.add(op)
    await db.commit()
    await db.refresh(op)

    background.add_task(
        staging_service.run_push_staging,
        request.app.state.sessionmaker,
        _settings(request),
        staging_site_id=staging_site.id,
        operation_id=op.id,
    )
    return OperationAccepted(site=SiteResponse.model_validate(production), operation_id=op.id)
