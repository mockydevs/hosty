"""Tenant ledger orchestration (v2/M0, ADR-013).

The `tenants` table is the source of truth for which Linux user and which
subuid/subgid range belongs to which client. This module allocates ranges
(monotonic, never reused while a row exists) and drives the host through
app.system.tenants. The DB row is committed BEFORE host mutation: a crash
between the two leaves a reserved range and an absent user, which
`ensure_tenant` repairs on the next call — never the reverse, where host
state exists that no ledger row explains.
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant, User
from app.system import tenants as tenants_sys

log = structlog.get_logger("hosty.tenancy")


class TenantLedgerError(RuntimeError):
    pass


async def _allocate_range(db: AsyncSession) -> tuple[int, int]:
    """Next disjoint subuid range. Derived from the ledger maximum so ranges
    are never reissued while any row holds one; the UNIQUE constraint on
    subuid_start makes a concurrent race lose at commit, not on the host."""
    current_max = (await db.execute(select(func.max(Tenant.subuid_start)))).scalar_one_or_none()
    if current_max is None:
        return tenants_sys.subid_range_for(0)
    next_start = current_max + tenants_sys.SUBID_COUNT
    return next_start, tenants_sys.SUBID_COUNT


async def get_tenant(db: AsyncSession, user_id: int) -> Tenant | None:
    return await db.get(Tenant, user_id)


async def ensure_tenant(db: AsyncSession, user: User) -> Tenant:
    """Idempotent: returns the existing tenant (repairing host state if
    needed) or allocates + provisions a new one."""
    tenant = await db.get(Tenant, user.id)
    if tenant is None:
        start, count = await _allocate_range(db)
        tenant = Tenant(
            user_id=user.id,
            linux_user=tenants_sys.linux_user_for(user.id),
            subuid_start=start,
            subuid_count=count,
        )
        db.add(tenant)
        # Ledger first: reserve the range before touching the host.
        await db.commit()
        await db.refresh(tenant)

    info = await tenants_sys.provision(
        tenant.linux_user,
        subuid_start=tenant.subuid_start,
        subuid_count=tenant.subuid_count,
    )
    if tenant.uid != info.uid:
        tenant.uid = info.uid
        await db.commit()
    log.info("tenant_ensured", linux_user=tenant.linux_user, uid=tenant.uid)
    return tenant


async def remove_tenant(db: AsyncSession, user_id: int) -> bool:
    """Tear down the host user, then release the ledger row (in that order:
    the range stays reserved until the host user is confirmed gone)."""
    tenant = await db.get(Tenant, user_id)
    if tenant is None:
        return False
    await tenants_sys.remove(tenant.linux_user)
    await db.delete(tenant)
    await db.commit()
    log.info("tenant_removed", linux_user=tenant.linux_user)
    return True
