"""Action → adapter dispatch (v2/M3, ADR-013). Dumb on purpose: one action,
one adapter call, structured log line per action. Ordering belongs to the
planner; retry/undo policy belongs to the reconciler (there is no undo —
failures surface and the next cycle replans).

Blocking filesystem work (stackhost) runs in a worker thread so a large
unit set never stalls the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.secrets import decrypt_secret
from app.db.models import SshKey, Tenant, User
from app.domain import actions as act
from app.services import tenancy
from app.system import quadlet, stackhost, systemd_user

log = structlog.get_logger("hosty.executor")


class ExecutorError(RuntimeError):
    pass


@dataclass
class ExecContext:
    """Everything action dispatch needs beyond the action itself.

    `sync_caddy` rebuilds + applies the full ingress config (injected: the
    config owner differs before/after M6). `tenant_in_use` answers "does
    DESIRED state still hold stacks for this tenant" — only the caller can
    see the whole desired set."""

    db: AsyncSession
    settings: Settings
    sync_caddy: Callable[[], Awaitable[None]]
    tenant_in_use: Callable[[str], Awaitable[bool]]


def _tenant_user_id(linux_user: str) -> int:
    return int(linux_user.rsplit("-", 1)[1])


async def _uid_for(db: AsyncSession, linux_user: str) -> int:
    row = (
        await db.execute(select(Tenant).where(Tenant.linux_user == linux_user))
    ).scalar_one_or_none()
    if row is None or row.uid is None:
        raise ExecutorError(f"Tenant {linux_user} is not in the ledger (EnsureTenant first?)")
    return row.uid


async def execute(action: act.Action, ctx: ExecContext) -> None:
    log.info("action_execute", action=type(action).__name__)
    match action:
        case act.EnsureTenant(tenant=tenant):
            user = await ctx.db.get(User, _tenant_user_id(tenant))
            if user is None:
                raise ExecutorError(f"No user account for tenant {tenant}")
            await tenancy.ensure_tenant(ctx.db, user)
            keys = (
                await ctx.db.execute(select(SshKey).where(SshKey.owner_id == user.id))
            ).scalars().all()
            await asyncio.to_thread(
                stackhost.sync_ssh_keys,
                tenant,
                [
                    (key.name, decrypt_secret(key.private_key_encrypted, ctx.settings.secret_key))
                    for key in keys
                ],
            )
        case act.EnsureVolumeDir(tenant=tenant, stack=stack, volume=volume):
            await asyncio.to_thread(stackhost.ensure_volume_dir, tenant, stack, volume)
        case act.WriteUnits(stack=spec):
            uid = await _uid_for(ctx.db, spec.tenant)
            await asyncio.to_thread(
                stackhost.sync_env_files, spec.tenant, spec.name, quadlet.env_files(spec)
            )
            await asyncio.to_thread(stackhost.sync_units, uid, spec.name, quadlet.unit_files(spec))
        case act.RemoveUnits(tenant=tenant, stack=stack):
            uid = await _uid_for(ctx.db, tenant)
            await asyncio.to_thread(stackhost.remove_units, uid, stack)
            await asyncio.to_thread(stackhost.remove_env_files, tenant, stack)
        case act.DaemonReload(tenant=tenant):
            await systemd_user.daemon_reload(tenant)
        case act.StartService(tenant=tenant, stack=stack, service=service):
            await systemd_user.control(tenant, "start", quadlet.service_unit_name(stack, service))
        case act.StopService(tenant=tenant, stack=stack, service=service):
            await systemd_user.control(tenant, "stop", quadlet.service_unit_name(stack, service))
        case act.RestartService(tenant=tenant, stack=stack, service=service):
            await systemd_user.control(tenant, "restart", quadlet.service_unit_name(stack, service))
        case act.RemoveVolumeDir(tenant=tenant, stack=stack, volume=volume):
            await stackhost.remove_volume_dir(tenant, stack, volume)
        case act.RemoveTenantIfEmpty(tenant=tenant):
            await _remove_tenant_if_empty(tenant, ctx)
        case act.SyncCaddy():
            await ctx.sync_caddy()
        case _:  # pragma: no cover - the Action union is closed
            raise ExecutorError(f"Unknown action: {action!r}")


async def _remove_tenant_if_empty(tenant: str, ctx: ExecContext) -> None:
    if await ctx.tenant_in_use(tenant):
        return
    row = (
        await ctx.db.execute(select(Tenant).where(Tenant.linux_user == tenant))
    ).scalar_one_or_none()
    if row is None:
        return  # never provisioned (or already released)
    if row.uid is not None:
        # Belt and braces: never delete a tenant user that still has host
        # artifacts — a unit file or volume left behind means a bug or an
        # in-flight teardown; keep the user and let the next cycle decide.
        scan = await asyncio.to_thread(stackhost.scan_tenant, row.uid, tenant)
        if scan.unit_files or scan.volume_dirs:
            log.warning("tenant_gc_skipped_artifacts_remain", tenant=tenant)
            return
    await tenancy.remove_tenant(ctx.db, row.user_id)
