"""Build the planner's Observed world from the real host (v2/M3, ADR-013).

Sources, per ledger tenant: quadlet unit files (presence + spec-hash
markers via stackhost), container liveness (podman ps over the tenant
socket — quadlet runs containers with --rm, so "exists and running" is the
activity signal), volume directories, and host-user presence.

Total: any tenant/source that fails to answer degrades to "absent", which
the planner repairs — observation must never crash a reconcile cycle.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant
from app.domain.specs import Observed, ObservedStack, ObservedUnit
from app.system import podman, stackhost
from app.system import tenants as tenants_sys

log = structlog.get_logger("hosty.observer")


async def observe(db: AsyncSession) -> Observed:
    observed: Observed = {}
    rows = (await db.execute(select(Tenant))).scalars().all()
    for row in rows:
        if row.uid is None:
            continue  # reserved range, host user never provisioned
        try:
            observed.update(await _observe_tenant(row.linux_user, row.uid))
        except Exception as exc:
            log.warning("observe_tenant_failed", tenant=row.linux_user, error=str(exc))
    return observed


async def _observe_tenant(linux_user: str, uid: int) -> Observed:
    tenant_present = await tenants_sys.exists(linux_user)
    scan = await asyncio.to_thread(stackhost.scan_tenant, uid, linux_user)
    containers = await podman.ps(uid) if tenant_present else []
    running = {c.name for c in containers if c.running}

    units: dict[str, dict[str, ObservedUnit]] = {}
    build_units: dict[str, dict[str, str | None]] = {}
    stacks_seen: set[str] = set()
    for unit_file in scan.unit_files:
        stacks_seen.add(unit_file.stack)
        if unit_file.service is None:
            continue  # .network file: presence only
        if unit_file.file_name.endswith(".build"):
            build_units.setdefault(unit_file.stack, {})[unit_file.service] = unit_file.spec_hash
            continue
        units.setdefault(unit_file.stack, {})[unit_file.service] = ObservedUnit(
            spec_hash=unit_file.spec_hash,
            active=f"{unit_file.stack}-{unit_file.service}" in running,
        )
    # Orphans: a labelled container running without its unit file (systemd
    # keeps removed units alive until stopped). Attribute by the hosty.stack
    # LABEL — container names are ambiguous when slugs contain hyphens.
    for container in containers:
        if not container.running or container.stack is None:
            continue
        service = container.name.removeprefix(f"{container.stack}-")
        units.setdefault(container.stack, {}).setdefault(
            service, ObservedUnit(spec_hash=None, active=True)
        )
        stacks_seen.add(container.stack)
    stacks_seen.update(scan.volume_dirs)

    return {
        stack: ObservedStack(
            tenant=linux_user,
            tenant_present=tenant_present,
            units=units.get(stack, {}),
            build_units=build_units.get(stack, {}),
            volume_dirs=scan.volume_dirs.get(stack, frozenset()),
        )
        for stack in stacks_seen
    }
