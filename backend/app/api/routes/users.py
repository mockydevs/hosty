"""User management API (Phase 11a/11d, admin-only).

Admins create client accounts with a temporary password (shown exactly once,
forced change on first login), suspend/unsuspend them, set per-resource
quotas, and delete them (choosing what happens to their sites).
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.clock import utcnow
from app.core.errors import ConflictError, NotFoundError
from app.core.security import create_access_token, hash_password
from app.db.models import AuditLog, Database, Operation, Plan, RefreshToken, Site, Stack, User
from app.services import mail, quotas
from app.services import sites as sites_service
from app.services.sites import DELETE_STEPS, initial_steps
from app.system import slices

log = structlog.get_logger("hosty.users")

router = APIRouter(dependencies=[Depends(require_admin)])

PASSWORD_MIN = 12
PASSWORD_MAX = 128


class UserAdminResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    username: str
    email: str | None = None
    phone: str | None = None
    role: str
    suspended: bool
    must_change_password: bool
    totp_enabled: bool = False
    max_sites: int | None
    max_databases: int | None
    max_apps: int | None = None
    max_stacks: int | None = None
    max_disk_mb: int | None = None
    cpu_quota_percent: int | None = None
    memory_max_mb: int | None = None
    plan_id: int | None = None
    plan_name: str | None = None
    site_count: int
    database_count: int
    created_at: datetime


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    # Omit to have the panel generate a strong temporary password.
    password: str | None = Field(default=None, min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    max_sites: int | None = Field(default=None, ge=0, le=1000)
    max_databases: int | None = Field(default=None, ge=0, le=1000)
    max_apps: int | None = Field(default=None, ge=0, le=1000)
    max_stacks: int | None = Field(default=None, ge=0, le=1000)
    plan_id: int | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        return mail.normalize_email(value)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        phone = value.strip()
        return phone or None


class CreatedUserResponse(BaseModel):
    user: UserAdminResponse
    # Shown exactly once — the panel stores only the Argon2id hash.
    temp_password: str | None = None
    email_sent: bool = False


class UpdateUserRequest(BaseModel):
    suspended: bool | None = None
    max_sites: int | None = Field(default=None, ge=0, le=1000)
    max_databases: int | None = Field(default=None, ge=0, le=1000)
    max_apps: int | None = Field(default=None, ge=0, le=1000)
    max_stacks: int | None = Field(default=None, ge=0, le=1000)
    max_disk_mb: int | None = Field(default=None, ge=1, le=1048576)
    cpu_quota_percent: int | None = Field(default=None, ge=1, le=1600)
    memory_max_mb: int | None = Field(default=None, ge=16, le=1048576)
    plan_id: int | None = None
    clear_max_sites: bool = False
    clear_max_databases: bool = False
    clear_max_apps: bool = False
    clear_max_stacks: bool = False
    clear_max_disk_mb: bool = False
    clear_cpu_quota_percent: bool = False
    clear_memory_max_mb: bool = False
    clear_plan: bool = False
    # Phase 11d: admin escape hatch for a client locked out of their 2FA.
    reset_totp: bool = False


class DeleteUserRequest(BaseModel):
    # reassign: sites move to the acting admin; delete_sites: full teardown.
    mode: Literal["reassign", "delete_sites"] = "reassign"
    confirm_username: str


async def _counts(db: AsyncSession, user_ids: list[int]) -> dict[int, tuple[int, int]]:
    """user_id -> (site_count, database_count)."""
    out: dict[int, tuple[int, int]] = dict.fromkeys(user_ids, (0, 0))  # type: ignore[arg-type]
    site_rows = (
        await db.execute(select(Site.owner_id, func.count()).group_by(Site.owner_id))
    ).all()
    db_rows = (
        await db.execute(
            select(Site.owner_id, func.count())
            .select_from(Database)
            .join(Site, Site.id == Database.site_id)
            .group_by(Site.owner_id)
        )
    ).all()
    sites_by_owner = {owner: n for owner, n in site_rows if owner is not None}
    dbs_by_owner = {owner: n for owner, n in db_rows if owner is not None}
    for uid in user_ids:
        out[uid] = (sites_by_owner.get(uid, 0), dbs_by_owner.get(uid, 0))
    return out


def _response(user: User, counts: tuple[int, int], plan: Plan | None = None) -> UserAdminResponse:
    return UserAdminResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        phone=user.phone,
        role=user.role,
        suspended=user.suspended,
        must_change_password=user.must_change_password,
        totp_enabled=user.totp_enabled,
        max_sites=user.max_sites,
        max_databases=user.max_databases,
        max_apps=user.max_apps,
        max_stacks=user.max_stacks,
        max_disk_mb=user.max_disk_mb,
        cpu_quota_percent=user.cpu_quota_percent,
        memory_max_mb=user.memory_max_mb,
        plan_id=user.plan_id,
        plan_name=plan.name if plan is not None else None,
        site_count=counts[0],
        database_count=counts[1],
        created_at=user.created_at,
    )


async def _plan_of(db: AsyncSession, user: User) -> Plan | None:
    return await db.get(Plan, user.plan_id) if user.plan_id is not None else None


async def _resync_slices(db: AsyncSession, user: User) -> None:
    """Phase 11c: re-apply systemd slices for every site the user owns after a
    limits/plan change. Best-effort — slice failures never fail the request."""
    limits = await quotas.effective_limits(db, user)
    rows = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalars().all()
    for site in rows:
        await slices.install_slice(
            site.site_user,
            cpu_quota_percent=limits.cpu_quota_percent,
            memory_max_mb=limits.memory_max_mb,
        )


async def _resync_caddy_safe(request: Request) -> None:
    """Phase 11d: republish vhosts (suspended owners get 503 pages). Best-effort:
    Caddy being briefly unreachable must not undo a suspension."""
    try:
        async with request.app.state.sessionmaker() as db:
            await sites_service.resync_caddy(db, request.app.state.settings)
    except Exception as exc:
        log.warning("suspension_caddy_resync_failed", error=str(exc))


async def _get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return user


async def _revoke_sessions(db: AsyncSession, user_id: int) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await db.execute(
        update(User).where(User.id == user_id).values(token_version=User.token_version + 1)
    )


@router.get("", response_model=list[UserAdminResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Any:
    users = (await db.execute(select(User).order_by(User.username).limit(limit).offset(offset))).scalars().all()
    counts = await _counts(db, [u.id for u in users])
    plans = {p.id: p for p in (await db.execute(select(Plan))).scalars().all()}
    return [_response(u, counts[u.id], plans.get(u.plan_id or -1)) for u in users]


@router.post("", response_model=CreatedUserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: Request, body: CreateUserRequest, db: AsyncSession = Depends(get_db)
) -> Any:
    existing = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A user named {body.username} already exists")
    if body.email is not None:
        existing_email = (
            await db.execute(select(User).where(User.email == body.email))
        ).scalar_one_or_none()
        if existing_email is not None:
            raise ConflictError(f"A user with email {body.email} already exists")
    plan: Plan | None = None
    if body.plan_id is not None:
        plan = await db.get(Plan, body.plan_id)
        if plan is None:
            raise NotFoundError("Plan not found")
    temp_password = body.password or secrets.token_urlsafe(15)
    user = User(
        username=body.username,
        email=body.email,
        phone=body.phone,
        password_hash=hash_password(temp_password),
        role="client",
        must_change_password=True,
        max_sites=body.max_sites,
        max_databases=body.max_databases,
        max_apps=body.max_apps,
        max_stacks=body.max_stacks,
        plan_id=body.plan_id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    email_sent = await mail.send_user_temp_password(
        db, request.app.state.settings, user=user, temp_password=temp_password
    )
    return CreatedUserResponse(
        user=_response(user, (0, 0), plan),
        temp_password=None if email_sent else temp_password,
        email_sent=email_sent,
    )


@router.patch("/{user_id}", response_model=UserAdminResponse)
async def update_user(
    request: Request,
    user_id: int,
    body: UpdateUserRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Any:
    user = await _get_user(db, user_id)
    if user.role == "admin":
        raise ConflictError("Admin accounts cannot be suspended or limited")
    suspension_changed = body.suspended is not None and body.suspended != user.suspended
    if body.suspended is not None:
        user.suspended = body.suspended
        if body.suspended:
            await _revoke_sessions(db, user.id)
    if body.clear_max_sites:
        user.max_sites = None
    elif body.max_sites is not None:
        user.max_sites = body.max_sites
    if body.clear_max_databases:
        user.max_databases = None
    elif body.max_databases is not None:
        user.max_databases = body.max_databases
    if body.clear_max_apps:
        user.max_apps = None
    elif body.max_apps is not None:
        user.max_apps = body.max_apps
    if body.clear_max_stacks:
        user.max_stacks = None
    elif body.max_stacks is not None:
        user.max_stacks = body.max_stacks
    if body.clear_max_disk_mb:
        user.max_disk_mb = None
    elif body.max_disk_mb is not None:
        user.max_disk_mb = body.max_disk_mb
    resource_limits_changed = (
        body.cpu_quota_percent is not None
        or body.memory_max_mb is not None
        or body.clear_cpu_quota_percent
        or body.clear_memory_max_mb
        or body.plan_id is not None
        or body.clear_plan
    )
    if body.clear_cpu_quota_percent:
        user.cpu_quota_percent = None
    elif body.cpu_quota_percent is not None:
        user.cpu_quota_percent = body.cpu_quota_percent
    if body.clear_memory_max_mb:
        user.memory_max_mb = None
    elif body.memory_max_mb is not None:
        user.memory_max_mb = body.memory_max_mb
    if body.clear_plan:
        user.plan_id = None
    elif body.plan_id is not None:
        if await db.get(Plan, body.plan_id) is None:
            raise NotFoundError("Plan not found")
        user.plan_id = body.plan_id
    if body.reset_totp:
        user.totp_enabled = False
        user.totp_secret_encrypted = None
        await _revoke_sessions(db, user.id)
    await db.commit()
    await db.refresh(user)
    if resource_limits_changed:
        await _resync_slices(db, user)
    if suspension_changed:
        # Take their sites offline with a 503 page (or bring them back).
        background.add_task(_resync_caddy_safe, request)
    counts = await _counts(db, [user.id])
    return _response(user, counts[user.id], await _plan_of(db, user))


@router.post("/{user_id}/reset-password", response_model=CreatedUserResponse)
async def reset_user_password(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Any:
    """Issue a new temporary password (shown once); all sessions are revoked."""
    user = await _get_user(db, user_id)
    if user.id == admin.id:
        raise ConflictError("Change your own password in Settings instead")
    if user.role == "admin":
        raise ConflictError("Other admin accounts cannot be reset from here")
    temp_password = secrets.token_urlsafe(15)
    user.password_hash = hash_password(temp_password)
    user.must_change_password = True
    user.password_changed_at = utcnow()
    await _revoke_sessions(db, user.id)
    await db.commit()
    await db.refresh(user)
    counts = await _counts(db, [user.id])
    email_sent = await mail.send_user_temp_password(
        db, request.app.state.settings, user=user, temp_password=temp_password
    )
    return CreatedUserResponse(
        user=_response(user, counts[user.id]),
        temp_password=None if email_sent else temp_password,
        email_sent=email_sent,
    )


@router.delete("/{user_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_user(
    request: Request,
    user_id: int,
    body: DeleteUserRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a client account.

    mode=reassign: their sites (and everything attached) move to the acting
    admin. mode=delete_sites: a delete pipeline is started for every site
    (async, same as deleting a site by hand); ownership moves to the acting
    admin while the teardown runs so records never dangle.
    """
    user = await _get_user(db, user_id)
    if user.id == admin.id:
        raise ConflictError("You cannot delete your own account")
    if user.role == "admin":
        raise ConflictError("Admin accounts cannot be deleted")
    if body.confirm_username.strip() != user.username:
        raise ConflictError("Confirmation does not match the username")

    sites = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalars().all()
    operation_ids: list[int] = []
    jobs: list[tuple[int, int]] = []
    for site in sites:
        site.owner_id = admin.id  # never leave dangling ownership
        if body.mode == "delete_sites" and site.status != "deleting":
            site.status = "deleting"
            op = Operation(
                kind="delete_site",
                site_id=site.id,
                domain=site.domain,
                steps_json=initial_steps(DELETE_STEPS),
            )
            db.add(op)
            await db.flush()
            operation_ids.append(op.id)
            jobs.append((site.id, op.id))
    active_stacks = (await db.execute(
        select(Stack).where(Stack.owner_id == user_id).where(Stack.status != "deleting")
    )).scalars().all()
    if active_stacks:
        raise ConflictError(f"User has {len(active_stacks)} active stack(s). Delete them first.")
    await _revoke_sessions(db, user.id)
    await db.delete(user)
    await db.commit()

    for site_id, operation_id in jobs:
        background.add_task(
            sites_service.run_delete_site,
            request.app.state.sessionmaker,
            request.app.state.settings,
            site_id=site_id,
            operation_id=operation_id,
        )
    return {
        "deleted": user_id,
        "mode": body.mode,
        "reassigned_sites": len(sites) if body.mode == "reassign" else 0,
        "operation_ids": operation_ids,
    }


# --- impersonation (Phase 11d) -------------------------------------------------------


class ImpersonateResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    username: str


@router.post("/{user_id}/impersonate", response_model=ImpersonateResponse)
async def impersonate_user(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ImpersonateResponse:
    """Issue a short-lived access token acting as the client (support tool).

    Loudly audited: this POST lands in the audit log, every action taken with
    the token is attributed as "admin (as client)", and the client UI shows a
    persistent banner. No refresh token is issued — the session simply expires.
    """
    user = await _get_user(db, user_id)
    if user.role == "admin":
        raise ConflictError("Impersonating another admin is not allowed")
    settings = request.app.state.settings
    token = create_access_token(
        subject=str(user.id),
        secret=settings.secret_key,
        ttl_seconds=settings.access_token_ttl_seconds,
        extra_claims={
            "imp": admin.id,
            "imp_ver": admin.token_version,
            "ver": user.token_version,
        },
    )
    log.info("impersonation_started", admin=admin.username, client=user.username)
    return ImpersonateResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_seconds,
        username=user.username,
    )


# --- failed logins (Phase 11d) -------------------------------------------------------


class FailedLoginResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    username: str | None
    status_code: int
    client_ip: str | None
    created_at: datetime


@router.get("/failed-logins", response_model=list[FailedLoginResponse])
async def failed_logins(limit: int = 100, db: AsyncSession = Depends(get_db)) -> Any:
    """Recent failed login attempts (401) and rate-limited bursts (429), straight
    from the audit log the middleware already writes."""
    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.path.in_(("/api/auth/login", "/api/auth/login/totp")),
                    AuditLog.status_code >= 400,
                )
                .order_by(AuditLog.created_at.desc())
                .limit(min(max(limit, 1), 500))
            )
        )
        .scalars()
        .all()
    )
    return rows
