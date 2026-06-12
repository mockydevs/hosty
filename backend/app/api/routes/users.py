"""User management API (Phase 11a, admin-only).

Admins create client accounts with a temporary password (shown exactly once,
forced change on first login), suspend/unsuspend them, set per-resource
quotas, and delete them (choosing what happens to their sites).
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.clock import utcnow
from app.core.errors import ConflictError, NotFoundError
from app.core.security import hash_password
from app.db.models import Database, Operation, RefreshToken, Site, User
from app.services import sites as sites_service
from app.services.sites import DELETE_STEPS, initial_steps

router = APIRouter(dependencies=[Depends(require_admin)])

PASSWORD_MIN = 12
PASSWORD_MAX = 128


class UserAdminResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    username: str
    role: str
    suspended: bool
    must_change_password: bool
    max_sites: int | None
    max_databases: int | None
    site_count: int
    database_count: int
    created_at: datetime


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    # Omit to have the panel generate a strong temporary password.
    password: str | None = Field(default=None, min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    max_sites: int | None = Field(default=None, ge=0, le=1000)
    max_databases: int | None = Field(default=None, ge=0, le=1000)


class CreatedUserResponse(BaseModel):
    user: UserAdminResponse
    # Shown exactly once — the panel stores only the Argon2id hash.
    temp_password: str


class UpdateUserRequest(BaseModel):
    suspended: bool | None = None
    max_sites: int | None = Field(default=None, ge=0, le=1000)
    max_databases: int | None = Field(default=None, ge=0, le=1000)
    clear_max_sites: bool = False
    clear_max_databases: bool = False


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


def _response(user: User, counts: tuple[int, int]) -> UserAdminResponse:
    return UserAdminResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        suspended=user.suspended,
        must_change_password=user.must_change_password,
        max_sites=user.max_sites,
        max_databases=user.max_databases,
        site_count=counts[0],
        database_count=counts[1],
        created_at=user.created_at,
    )


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


@router.get("", response_model=list[UserAdminResponse])
async def list_users(db: AsyncSession = Depends(get_db)) -> Any:
    users = (await db.execute(select(User).order_by(User.username))).scalars().all()
    counts = await _counts(db, [u.id for u in users])
    return [_response(u, counts[u.id]) for u in users]


@router.post("", response_model=CreatedUserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest, db: AsyncSession = Depends(get_db)) -> Any:
    existing = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A user named {body.username} already exists")
    temp_password = body.password or secrets.token_urlsafe(15)
    user = User(
        username=body.username,
        password_hash=hash_password(temp_password),
        role="client",
        must_change_password=True,
        max_sites=body.max_sites,
        max_databases=body.max_databases,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return CreatedUserResponse(user=_response(user, (0, 0)), temp_password=temp_password)


@router.patch("/{user_id}", response_model=UserAdminResponse)
async def update_user(
    user_id: int,
    body: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    user = await _get_user(db, user_id)
    if user.role == "admin":
        raise ConflictError("Admin accounts cannot be suspended or limited")
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
    await db.commit()
    await db.refresh(user)
    counts = await _counts(db, [user.id])
    return _response(user, counts[user.id])


@router.post("/{user_id}/reset-password", response_model=CreatedUserResponse)
async def reset_user_password(
    user_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
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
    return CreatedUserResponse(user=_response(user, counts[user.id]), temp_password=temp_password)


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
