"""Sites API: list/create/delete sites, per-domain SSL status.

Provisioning is asynchronous: POST/DELETE return 202 with an operation id the
UI polls via /api/operations/{id}.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import Operation, Site
from app.services import sites as sites_service
from app.services import ssl as ssl_service
from app.services import wordpress as wordpress_service
from app.services.sites import (
    CREATE_STEPS,
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
    created_at: datetime


class CreateSiteRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    php_version: str | None = None

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
async def list_sites(db: AsyncSession = Depends(get_db)) -> Any:
    rows = (await db.execute(select(Site).order_by(Site.domain))).scalars().all()
    return rows


@router.get("/{site_id}", response_model=SiteResponse)
async def get_site(site_id: int, db: AsyncSession = Depends(get_db)) -> Any:
    site = await db.get(Site, site_id)
    if site is None:
        raise NotFoundError("Site not found")
    return site


@router.post("", response_model=OperationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_site(
    request: Request,
    body: CreateSiteRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Any:
    settings = _settings(request)
    php_version = body.php_version or settings.default_php_version
    if php_version not in settings.php_versions:
        raise DomainError(f"Unsupported PHP version: {php_version}")

    existing = (
        await db.execute(select(Site).where(Site.domain == body.domain))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A site for {body.domain} already exists")

    site = Site(
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
        steps_json=initial_steps(CREATE_STEPS),
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
    )
    return OperationAccepted(site=SiteResponse.model_validate(site), operation_id=op.id)


@router.delete("/{site_id}", response_model=OperationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def delete_site(
    request: Request,
    site_id: int,
    body: DeleteSiteRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Any:
    site = await db.get(Site, site_id)
    if site is None:
        raise NotFoundError("Site not found")
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
async def site_ssl_status(site_id: int, db: AsyncSession = Depends(get_db)) -> Any:
    site = await db.get(Site, site_id)
    if site is None:
        raise NotFoundError("Site not found")
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
) -> Any:
    """Re-apply the Caddy config so it (re)attempts certificate issuance.

    Caddy renews valid certificates on its own; this endpoint covers the
    retry cases (initial issuance failed, DNS fixed after creation) and
    returns a fresh probe of the resulting certificate status.
    """
    site = await _get_active_site(db, site_id)
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
) -> Any:
    """Toggle 'behind the Cloudflare proxy' for a site.

    When enabled, Caddy issues an internal origin certificate for the domain
    instead of attempting ACME HTTP-01 (which the proxy would break); Cloudflare
    terminates public TLS at the edge (SSL mode "Full").
    """
    site = await _get_active_site(db, site_id)
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


@operations_router.get("/{operation_id}", response_model=OperationResponse)
async def get_operation(operation_id: int, db: AsyncSession = Depends(get_db)) -> Any:
    op = await db.get(Operation, operation_id)
    if op is None:
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


async def _get_active_site(db: AsyncSession, site_id: int) -> Site:
    site = await db.get(Site, site_id)
    if site is None:
        raise NotFoundError("Site not found")
    if site.status != "active":
        raise ConflictError(f"Site is {site.status}; wait until it is active")
    return site


@router.post("/{site_id}/php", response_model=SiteResponse)
async def change_php_version(
    request: Request,
    site_id: int,
    body: ChangePhpVersionRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    settings = _settings(request)
    if body.php_version not in settings.php_versions:
        raise DomainError(f"Unsupported PHP version: {body.php_version}")
    site = await _get_active_site(db, site_id)
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
) -> Any:
    site = await _get_active_site(db, site_id)
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
async def wordpress_status(site_id: int, db: AsyncSession = Depends(get_db)) -> Any:
    site = await db.get(Site, site_id)
    if site is None:
        raise NotFoundError("Site not found")
    result = await wordpress_service.status(site)
    return WpStatusResponse(
        installed=result.installed,
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
) -> Any:
    site = await _get_active_site(db, site_id)
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
async def wordpress_action(site_id: int, action: str, db: AsyncSession = Depends(get_db)) -> Any:
    site = await _get_active_site(db, site_id)
    if not site.wordpress:
        raise ConflictError("WordPress is not installed on this site")
    if action not in wordpress_service.WP_ACTIONS:
        raise NotFoundError(f"Unknown action: {action}")
    url = await wordpress_service.run_action(site, action)
    return WpActionResponse(action=action, url=url)
