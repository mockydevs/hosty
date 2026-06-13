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
from app.system import podman, quadlet, stackhost, systemd_user

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
                (await ctx.db.execute(select(SshKey).where(SshKey.owner_id == user.id)))
                .scalars()
                .all()
            )
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
            # Safety net: force-remove any leftover container for this stack so
            # an orphan can't keep its published port bound (the next stack to
            # reuse that port would fail with "address already in use").
            await podman.remove_stack_containers(uid, stack)
        case act.DaemonReload(tenant=tenant):
            await systemd_user.daemon_reload(tenant)
        case act.StartService(tenant=tenant, stack=stack, service=service):
            await _control_service(ctx, tenant, stack, service, "start")
        case act.StopService(tenant=tenant, stack=stack, service=service):
            await systemd_user.control(tenant, "stop", quadlet.service_unit_name(stack, service))
        case act.RestartService(tenant=tenant, stack=stack, service=service):
            await _control_service(ctx, tenant, stack, service, "restart")
        case act.RemoveVolumeDir(tenant=tenant, stack=stack, volume=volume):
            await stackhost.remove_volume_dir(tenant, stack, volume)
        case act.RemoveTenantIfEmpty(tenant=tenant):
            await _remove_tenant_if_empty(tenant, ctx)
        case act.SyncCaddy():
            await ctx.sync_caddy()
        case _:  # pragma: no cover - the Action union is closed
            raise ExecutorError(f"Unknown action: {action!r}")


# systemctl reports a unit it cannot see as "not found"/"not loaded". After
# WriteUnits + DaemonReload both succeeded, that usually means the podman
# quadlet user-generator has not YET turned the written unit file into a
# .service: a freshly lingered tenant user manager races the first
# daemon-reload, so the reload returns before generation completes (the unit
# file is on disk, the generator just hasn't run over it). The cure is to
# re-run the generator and retry — once the manager is warm this is reliable.
_UNIT_MISSING_HINTS = ("not found", "not loaded")
# Backoff between regenerate+retry attempts. Bounded: a unit still missing
# after this is a real host fault (missing user-generator / podman too old),
# not a startup race, and is surfaced with an actionable message.
_REGENERATE_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)


def _is_unit_missing(exc: Exception) -> bool:
    return any(hint in str(exc).lower() for hint in _UNIT_MISSING_HINTS)


async def _control_service(
    ctx: ExecContext, tenant: str, stack: str, service: str, action: str
) -> None:
    unit = quadlet.service_unit_name(stack, service)
    last_exc: systemd_user.SystemdUserError | None = None
    for delay in (None, *_REGENERATE_RETRY_DELAYS):
        if delay is not None:
            # The unit was missing on the previous attempt: give the user
            # manager a moment, re-run the quadlet generator, then retry.
            await asyncio.sleep(delay)
            await systemd_user.daemon_reload(tenant)
        try:
            await systemd_user.control(tenant, action, unit)
            return
        except systemd_user.SystemdUserError as exc:
            if not _is_unit_missing(exc):
                raise  # a genuine start failure (image pull, crash loop, …) — surface as-is
            last_exc = exc
            log.warning("unit_not_generated_retrying", unit=unit, tenant=tenant, action=action)
    uid = await _uid_for(ctx.db, tenant)
    raise ExecutorError(
        f"{unit} was not generated by the podman quadlet user-generator after "
        f"{len(_REGENERATE_RETRY_DELAYS)} daemon-reload retries. Its unit file is in "
        f"{quadlet.unit_dir(uid)}/, so this is a host fault, not a startup race: verify "
        f"podman >= 4.6 (with /etc/containers/systemd/users support), that the "
        f"podman-user-generator is installed, and that the user manager for {tenant} "
        f"is running. Original error: {last_exc}"
    ) from last_exc


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
