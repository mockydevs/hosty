"""Effective quota/limit resolution (Phase 11c/11d).

A client's limits come from two layers: an optional named Plan supplies the
defaults, and explicit per-user values always override the plan. None means
unlimited at both layers. Admins are never limited.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, User


@dataclass(frozen=True)
class EffectiveLimits:
    max_sites: int | None = None
    max_databases: int | None = None
    max_disk_mb: int | None = None
    cpu_quota_percent: int | None = None
    memory_max_mb: int | None = None


UNLIMITED = EffectiveLimits()


def resolve(user: User, plan: Plan | None) -> EffectiveLimits:
    """Pure: explicit user value wins; otherwise the plan's; otherwise unlimited."""
    if user.role == "admin":
        return UNLIMITED

    def pick(explicit: int | None, from_plan: int | None) -> int | None:
        return explicit if explicit is not None else from_plan

    return EffectiveLimits(
        max_sites=pick(user.max_sites, plan.max_sites if plan else None),
        max_databases=pick(user.max_databases, plan.max_databases if plan else None),
        max_disk_mb=pick(user.max_disk_mb, plan.max_disk_mb if plan else None),
        cpu_quota_percent=pick(user.cpu_quota_percent, plan.cpu_quota_percent if plan else None),
        memory_max_mb=pick(user.memory_max_mb, plan.memory_max_mb if plan else None),
    )


async def effective_limits(db: AsyncSession, user: User) -> EffectiveLimits:
    plan = await db.get(Plan, user.plan_id) if user.plan_id is not None else None
    return resolve(user, plan)
