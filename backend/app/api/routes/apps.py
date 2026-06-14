"""Apps API (Phase 12a): containerized apps — list/create/delete, lifecycle,
logs. Provisioning is asynchronous like sites: POST/DELETE return 202 with an
operation id the UI polls via /api/operations/{id}."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, is_admin
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import App, Operation, Site, User
from app.services import apps as apps_service
from app.services import quotas
from app.services.apps import AppValidationError, validate_app_name
from app.services.sites import DomainValidationError, initial_steps, validate_domain
from app.system import docker

router = APIRouter(dependencies=[Depends(get_current_user)])


class AppRequestError(AppError):
    status_code = 422
    code = "invalid_app"


class AppsDisabledError(AppError):
    status_code = 409
    code = "apps_disabled"


class VolumeSpec(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    mount_path: str = Field(min_length=1, max_length=255)


class AppResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    domain: str
    image: str
    image_digest: str | None
    internal_port: int
    host_port: int
    memory_mb: int | None
    cpu_percent: int | None
    status: str
    error_message: str | None
    created_at: datetime


class CreateAppRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    domain: str = Field(min_length=1, max_length=253)
    image: str = Field(min_length=1, max_length=512)
    internal_port: int = Field(ge=1, le=65535)
    env: dict[str, str] = Field(default_factory=dict)
    volumes: list[VolumeSpec] = Field(default_factory=list)
    memory_mb: int | None = Field(default=None, ge=64, le=65536)
    cpu_percent: int | None = Field(default=None, ge=10, le=1600)

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, v: str) -> str:
        try:
            return validate_domain(v)
        except DomainValidationError as exc:
            raise ValueError(str(exc)) from exc


class DeleteAppRequest(BaseModel):
    confirm_name: str


class AppOperationAccepted(BaseModel):
    app: AppResponse
    operation_id: int


class AppLogsResponse(BaseModel):
    logs: str


def _settings(request: Request):
    return request.app.state.settings


def _require_enabled(request: Request) -> None:
    if not _settings(request).apps_enabled:
        raise AppsDisabledError("Containerized apps are disabled on this server")


async def fetch_owned_app(db: AsyncSession, user: User, app_id: int) -> App:
    """Admins see everything; clients get a 404 (not 403) for other tenants'
    apps so existence never leaks — same contract as fetch_owned_site."""
    app = await db.get(App, app_id)
    if app is None or (not is_admin(user) and app.owner_id != user.id):
        raise NotFoundError("App not found")
    return app


@router.get("", response_model=list[AppResponse])
async def list_apps(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> Any:
    query = select(App).order_by(App.name)
    if not is_admin(user):
        query = query.where(App.owner_id == user.id)
    return (await db.execute(query)).scalars().all()


@router.get("/{app_id}", response_model=AppResponse)
async def get_app(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    return await fetch_owned_app(db, user, app_id)


@router.post("", response_model=AppOperationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_app(
    request: Request,
    body: CreateAppRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    _require_enabled(request)
    settings = _settings(request)
    try:
        name = validate_app_name(body.name)
        docker.validate_image_ref(body.image)
        env = apps_service.validate_env(body.env)
        volumes = apps_service.validate_volumes([v.model_dump() for v in body.volumes])
    except (AppValidationError, docker.InvalidDockerArgError) as exc:
        raise AppRequestError(str(exc)) from exc

    limits = await quotas.effective_limits(db, user)
    if not is_admin(user) and limits.max_apps is not None:
        owned = (
            await db.execute(select(func.count()).select_from(App).where(App.owner_id == user.id))
        ).scalar_one()
        if owned >= limits.max_apps:
            raise ConflictError(f"App quota reached ({limits.max_apps})")

    # One domain, one route: collisions with sites AND other apps are refused.
    existing_app = (
        await db.execute(select(App).where((App.domain == body.domain) | (App.name == name)))
    ).scalar_one_or_none()
    if existing_app is not None:
        clash = "domain" if existing_app.domain == body.domain else "name"
        raise ConflictError(f"An app already uses this {clash}")
    existing_site = (
        await db.execute(select(Site).where(Site.domain == body.domain))
    ).scalar_one_or_none()
    if existing_site is not None:
        raise ConflictError(f"A site for {body.domain} already exists")

    app = App(
        owner_id=user.id,
        name=name,
        domain=body.domain,
        image=body.image,
        internal_port=body.internal_port,
        host_port=await apps_service.allocate_host_port(db, settings),
        env_encrypted=apps_service.encrypt_env(env, settings),
        volumes_json=json.dumps(volumes),
        memory_mb=body.memory_mb,
        cpu_percent=body.cpu_percent,
        status="provisioning",
    )
    db.add(app)
    await db.flush()
    op = Operation(
        kind="create_app",
        domain=app.domain,
        steps_json=initial_steps(apps_service.CREATE_APP_STEPS),
    )
    db.add(op)
    try:
        await db.commit()
    except IntegrityError:
        raise ConflictError("An app with this name or domain already exists")
    await db.refresh(app)
    await db.refresh(op)

    background.add_task(
        apps_service.run_create_app,
        request.app.state.sessionmaker,
        settings,
        app_id=app.id,
        operation_id=op.id,
    )
    return AppOperationAccepted(app=AppResponse.model_validate(app), operation_id=op.id)


@router.delete(
    "/{app_id}", response_model=AppOperationAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def delete_app(
    request: Request,
    app_id: int,
    body: DeleteAppRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    app = await fetch_owned_app(db, user, app_id)
    if body.confirm_name.strip().lower() != app.name:
        raise ConflictError("Confirmation does not match the app name")
    if app.status == "deleting":
        raise ConflictError("App is already being deleted")

    app.status = "deleting"
    op = Operation(
        kind="delete_app",
        domain=app.domain,
        steps_json=initial_steps(apps_service.DELETE_APP_STEPS),
    )
    db.add(op)
    await db.commit()
    await db.refresh(app)
    await db.refresh(op)

    background.add_task(
        apps_service.run_delete_app,
        request.app.state.sessionmaker,
        _settings(request),
        app_id=app.id,
        operation_id=op.id,
    )
    return AppOperationAccepted(app=AppResponse.model_validate(app), operation_id=op.id)


@router.post("/{app_id}/start", response_model=AppResponse)
async def start_app(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    app = await fetch_owned_app(db, user, app_id)
    if app.status in ("provisioning", "deleting"):
        raise ConflictError(f"App is {app.status}")
    await apps_service.start_app(db, app)
    return app


@router.post("/{app_id}/stop", response_model=AppResponse)
async def stop_app(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    app = await fetch_owned_app(db, user, app_id)
    if app.status in ("provisioning", "deleting"):
        raise ConflictError(f"App is {app.status}")
    await apps_service.stop_app(db, app)
    return app


@router.post("/{app_id}/restart", response_model=AppResponse)
async def restart_app(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    app = await fetch_owned_app(db, user, app_id)
    if app.status in ("provisioning", "deleting"):
        raise ConflictError(f"App is {app.status}")
    await apps_service.restart_app(db, app)
    return app


@router.get("/{app_id}/logs", response_model=AppLogsResponse)
async def app_logs(
    app_id: int,
    tail: int = Query(default=200, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    app = await fetch_owned_app(db, user, app_id)
    return AppLogsResponse(logs=await apps_service.app_logs(app, tail=tail))
