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


class SiteResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    domain: str
    site_user: str
    doc_root: str
    php_version: str
    status: str
    error_message: str | None
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
    result = await ssl_service.probe(site.domain)
    return CertStatusResponse(
        domain=result.domain,
        status=result.status,
        issuer=result.issuer,
        not_after=result.not_after,
        detail=result.detail,
    )


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
