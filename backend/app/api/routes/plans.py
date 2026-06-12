"""Plan management (Phase 11d, admin-only): named quota bundles for clients.

A plan supplies default quotas; explicit per-user values override it
(services/quotas.py). Deleting a plan that users are assigned to is refused —
reassign them first, so nobody's limits change silently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.errors import ConflictError, NotFoundError
from app.db.models import Plan, User

router = APIRouter(dependencies=[Depends(require_admin)])


class PlanResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    max_sites: int | None
    max_databases: int | None
    max_apps: int | None = None
    max_disk_mb: int | None
    cpu_quota_percent: int | None
    memory_max_mb: int | None
    user_count: int = 0
    created_at: datetime


class UpsertPlanRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    max_sites: int | None = Field(default=None, ge=0, le=1000)
    max_databases: int | None = Field(default=None, ge=0, le=1000)
    max_apps: int | None = Field(default=None, ge=0, le=1000)
    max_disk_mb: int | None = Field(default=None, ge=1, le=1048576)
    cpu_quota_percent: int | None = Field(default=None, ge=1, le=1600)
    memory_max_mb: int | None = Field(default=None, ge=16, le=1048576)


async def _user_counts(db: AsyncSession) -> dict[int, int]:
    rows = (
        await db.execute(
            select(User.plan_id, func.count())
            .where(User.plan_id.is_not(None))
            .group_by(User.plan_id)
        )
    ).all()
    return {plan_id: n for plan_id, n in rows}


def _response(plan: Plan, user_count: int) -> PlanResponse:
    return PlanResponse(
        id=plan.id,
        name=plan.name,
        max_sites=plan.max_sites,
        max_databases=plan.max_databases,
        max_apps=plan.max_apps,
        max_disk_mb=plan.max_disk_mb,
        cpu_quota_percent=plan.cpu_quota_percent,
        memory_max_mb=plan.memory_max_mb,
        user_count=user_count,
        created_at=plan.created_at,
    )


@router.get("", response_model=list[PlanResponse])
async def list_plans(db: AsyncSession = Depends(get_db)) -> Any:
    plans = (await db.execute(select(Plan).order_by(Plan.name))).scalars().all()
    counts = await _user_counts(db)
    return [_response(p, counts.get(p.id, 0)) for p in plans]


@router.post("", response_model=PlanResponse, status_code=status.HTTP_201_CREATED)
async def create_plan(body: UpsertPlanRequest, db: AsyncSession = Depends(get_db)) -> Any:
    existing = (
        await db.execute(select(Plan).where(Plan.name == body.name.strip()))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A plan named {body.name!r} already exists")
    plan = Plan(
        name=body.name.strip(),
        max_sites=body.max_sites,
        max_databases=body.max_databases,
        max_apps=body.max_apps,
        max_disk_mb=body.max_disk_mb,
        cpu_quota_percent=body.cpu_quota_percent,
        memory_max_mb=body.memory_max_mb,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return _response(plan, 0)


@router.put("/{plan_id}", response_model=PlanResponse)
async def update_plan(
    plan_id: int, body: UpsertPlanRequest, db: AsyncSession = Depends(get_db)
) -> Any:
    plan = await db.get(Plan, plan_id)
    if plan is None:
        raise NotFoundError("Plan not found")
    clash = (
        await db.execute(select(Plan).where(Plan.name == body.name.strip(), Plan.id != plan_id))
    ).scalar_one_or_none()
    if clash is not None:
        raise ConflictError(f"A plan named {body.name!r} already exists")
    plan.name = body.name.strip()
    plan.max_sites = body.max_sites
    plan.max_databases = body.max_databases
    plan.max_apps = body.max_apps
    plan.max_disk_mb = body.max_disk_mb
    plan.cpu_quota_percent = body.cpu_quota_percent
    plan.memory_max_mb = body.memory_max_mb
    await db.commit()
    await db.refresh(plan)
    counts = await _user_counts(db)
    return _response(plan, counts.get(plan.id, 0))


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: int, db: AsyncSession = Depends(get_db)) -> None:
    plan = await db.get(Plan, plan_id)
    if plan is None:
        raise NotFoundError("Plan not found")
    counts = await _user_counts(db)
    if counts.get(plan_id, 0) > 0:
        raise ConflictError(
            f"{counts[plan_id]} user(s) are on this plan — reassign them before deleting it"
        )
    await db.delete(plan)
    await db.commit()
