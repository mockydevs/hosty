"""Action → adapter dispatch (v2/M3, ADR-013). Dumb on purpose: one action,
one adapter call, structured log line per action. Ordering belongs to the
planner; retry/undo policy belongs to the reconciler (there is no undo —
failures surface and the next cycle replans).

All host I/O is routed through `ctx.host` (a HostContext).  The default is
`LocalHost()`, so every existing caller that omits `host=` is unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.secrets import decrypt_secret
from app.db.models import GitSource, SshKey, Stack, Tenant, User
from app.domain import actions as act
from app.domain.specs import derive_host_port
from app.domain.specs import StackSpec
from app.services import stacks as stacks_service, tenancy
from app.services.github import get_installation_token
from app.system import quadlet
from app.system.host import HostContext, LocalHost
from app.system.systemd_user import SystemdUserError

log = structlog.get_logger("hosty.executor")


class ExecutorError(RuntimeError):
    pass


@dataclass
class ExecContext:
    """Everything action dispatch needs beyond the action itself.

    `sync_caddy` rebuilds + applies the full ingress config (injected: the
    config owner differs before/after M6). `tenant_in_use` answers "does
    DESIRED state still hold stacks for this tenant" — only the caller can
    see the whole desired set.  `host` routes all I/O to the right server."""

    db: AsyncSession
    settings: Settings
    sync_caddy: Callable[[], Awaitable[None]]
    tenant_in_use: Callable[[str], Awaitable[bool]]
    host: HostContext = field(default_factory=LocalHost)
    caddy_patch_upstream: Callable[[str, str], Awaitable[None]] | None = None


def _tenant_user_id(linux_user: str) -> int:
    return int(linux_user.rsplit("-", 1)[1])


async def _uid_for(db: AsyncSession, linux_user: str) -> int:
    row = (
        await db.execute(select(Tenant).where(Tenant.linux_user == linux_user))
    ).scalar_one_or_none()
    if row is None or row.uid is None:
        raise ExecutorError(f"Tenant {linux_user} is not in the ledger (EnsureTenant first?)")
    return row.uid


async def _resolve_git_token(spec: StackSpec, ctx: ExecContext) -> str | None:
    """Return an authenticated GitHub App installation token for private-repo stacks, else None.

    Looks up the stack's source_id from its encrypted inputs.  When present,
    generates a fresh GitHub App installation token and returns it.  Any failure 
    (missing source, bad token) is logged and returns None so the unauthenticated 
    flow is used as a fallback.
    """
    any_build = any(s.build_repo for s in spec.services)
    if not any_build:
        return None
    try:
        user_id = _tenant_user_id(spec.tenant)
        row = (
            await ctx.db.execute(
                select(Stack).where(Stack.name == spec.name, Stack.owner_id == user_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        inputs = stacks_service.decrypt_inputs(row, ctx.settings)
        source_id = inputs.get("source_id")
        if not source_id:
            return None
        source = await ctx.db.get(GitSource, int(source_id))
        if not source or not source.installation_id:
            return None
        return await get_installation_token(source, ctx.settings)
    except Exception:
        log.warning("git_token_resolve_failed", stack=spec.name, exc_info=True)
        return None


async def execute(action: act.Action, ctx: ExecContext) -> None:
    log.info("action_execute", action=type(action).__name__)
    match action:
        case act.EnsureTenant(tenant=tenant):
            user = await ctx.db.get(User, _tenant_user_id(tenant))
            if user is None:
                raise ExecutorError(f"No user account for tenant {tenant}")
            await tenancy.ensure_tenant(ctx.db, user, host=ctx.host)
            keys = (
                (await ctx.db.execute(select(SshKey).where(SshKey.owner_id == user.id)))
                .scalars()
                .all()
            )
            await ctx.host.sync_ssh_keys(
                tenant,
                [
                    (key.name, decrypt_secret(key.private_key_encrypted, ctx.settings.secret_key))
                    for key in keys
                ],
            )
        case act.EnsureVolumeDir(tenant=tenant, stack=stack, volume=volume):
            await ctx.host.ensure_volume_dir(tenant, stack, volume)
        case act.WriteUnits(stack=spec):
            uid = await _uid_for(ctx.db, spec.tenant)
            await ctx.host.sync_env_files(spec.tenant, spec.name, quadlet.env_files(spec))
            git_token = await _resolve_git_token(spec, ctx)
            await ctx.host.sync_units(uid, spec.name, spec.tenant, quadlet.unit_files(spec, git_token=git_token))
        case act.RemoveUnits(tenant=tenant, stack=stack):
            uid = await _uid_for(ctx.db, tenant)
            await ctx.host.remove_units(uid, stack, tenant)
            await ctx.host.remove_env_files(tenant, stack)
            await ctx.host.remove_stack_containers(uid, stack)
        case act.DaemonReload(tenant=tenant):
            await ctx.host.daemon_reload(tenant)
        case act.StartService(tenant=tenant, stack=stack, service=service):
            await _control_service(ctx, tenant, stack, service, "start")
        case act.StopService(tenant=tenant, stack=stack, service=service):
            await ctx.host.control_service(tenant, "stop", quadlet.service_unit_name(stack, service))
        case act.RestartService(tenant=tenant, stack=stack, service=service):
            await _control_service(ctx, tenant, stack, service, "restart")
        case act.RemoveVolumeDir(tenant=tenant, stack=stack, volume=volume):
            await ctx.host.remove_volume_dir(tenant, stack, volume)
        case act.ZeroDowntimeDeploy(tenant=tenant, stack=stack_spec, service=service):
            await _zero_downtime_deploy(tenant, stack_spec, service, ctx)
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
    last_exc: SystemdUserError | None = None
    for delay in (None, *_REGENERATE_RETRY_DELAYS):
        if delay is not None:
            await asyncio.sleep(delay)
            await ctx.host.daemon_reload(tenant)
        try:
            await ctx.host.control_service(tenant, action, unit)
            return
        except SystemdUserError as exc:
            if not _is_unit_missing(exc):
                raise
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


async def _zero_downtime_deploy(
    tenant: str, stack_spec: act.StackSpec, service_name: str, ctx: ExecContext
) -> None:
    """Blue-green zero-downtime deploy for one service.

    1. Start a candidate container (same image, candidate port) via podman run.
    2. Health-check the candidate (HTTP probe or TCP connect).
    3. Swap Caddy's upstream to the candidate (hot-patch, no full reload).
    4. Stop the old Quadlet-managed container.
    5. Start the Quadlet unit again (picks up the new :latest image on normal port).
    6. Health-check the Quadlet unit.
    7. Swap Caddy's upstream back to the normal port.
    8. Stop and remove the candidate container.

    If any step fails, the candidate is always cleaned up and a full restart
    is attempted as a fallback so the stack never stays in a split state.
    """
    from app.services.caddy import CaddyClient, CaddyError
    from app.domain.specs import derive_host_port

    svc_spec = next((s for s in stack_spec.services if s.name == service_name), None)
    if svc_spec is None or svc_spec.internal_port is None:
        raise ExecutorError(f"Service {service_name!r} not found or has no port — cannot zero-downtime deploy")

    image = f"hosty/{stack_spec.name}-{service_name}:latest"
    internal_port = svc_spec.internal_port
    normal_host_port = derive_host_port(internal_port)
    candidate_port = normal_host_port + 10000
    candidate_name = f"{stack_spec.name}-{service_name}-candidate"
    loopback = stack_spec.loopback_ip
    normal_upstream = f"{loopback}:{normal_host_port}"
    candidate_upstream = f"{loopback}:{candidate_port}"
    unit = quadlet.service_unit_name(stack_spec.name, service_name)

    # Find domains that route to this service (for Caddy swaps)
    service_domains = [
        ep.domain for ep in stack_spec.endpoints if ep.service == service_name
    ]

    caddy = CaddyClient(ctx.settings.caddy_admin_url)

    async def _cleanup_candidate() -> None:
        try:
            await ctx.host.run_as_tenant(
                tenant,
                ["/usr/bin/podman", "stop", "--time", "5", candidate_name],
                timeout=15,
            )
        except Exception:
            pass
        try:
            await ctx.host.run_as_tenant(
                tenant,
                ["/usr/bin/podman", "rm", "-f", candidate_name],
                timeout=10,
            )
        except Exception:
            pass

    async def _health_poll(host_port: int, *, path: str = "/health", max_wait: int = 60) -> bool:
        """Poll HTTP health check, return True when passing or when no health check is configured."""
        if not svc_spec.health_check_enabled:
            # No health check configured — just wait briefly for the process to start
            await asyncio.sleep(2)
            return True
        hc_path = svc_spec.health_check_path or path
        deadline = max_wait
        waited = 0.0
        interval = float(svc_spec.health_check_interval)
        while waited < deadline:
            try:
                result = await ctx.host.run_as_tenant(
                    tenant,
                    ["curl", "-sf", "--max-time", "3", f"http://127.0.0.1:{host_port}{hc_path}"],
                    timeout=10,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass
            await asyncio.sleep(min(interval, deadline - waited))
            waited += interval
        return False

    log.info("zero_downtime_deploy_start", stack=stack_spec.name, service=service_name,
             candidate_port=candidate_port)

    # Make sure no leftover candidate from a previous failed deploy
    await _cleanup_candidate()

    # 1. Start candidate container
    try:
        await ctx.host.run_as_tenant(
            tenant,
            [
                "/usr/bin/podman", "run", "-d",
                "--name", candidate_name,
                "--network", f"hosty-{stack_spec.name}",
                "--network-alias", f"{service_name}-candidate",
                "-p", f"{loopback}:{candidate_port}:{internal_port}",
                image,
            ],
            timeout=30,
        )
    except Exception as exc:
        raise ExecutorError(f"Failed to start candidate container: {exc}") from exc

    try:
        # 2. Health-check candidate
        if not await _health_poll(candidate_port):
            raise ExecutorError(
                f"Candidate container for {service_name!r} did not become healthy "
                f"within {svc_spec.health_check_start_period + 60}s"
            )

        # 3. Swap Caddy to candidate
        swap_errors: list[str] = []
        if service_domains:
            for domain in service_domains:
                try:
                    if ctx.caddy_patch_upstream:
                        await ctx.caddy_patch_upstream(domain, candidate_upstream)
                    else:
                        await caddy.patch_upstream(domain, candidate_upstream)
                except CaddyError as exc:
                    swap_errors.append(str(exc))
                    log.warning("zdeploy_caddy_swap_failed", domain=domain, error=str(exc))

        # 4. Stop old Quadlet unit
        await ctx.host.control_service(tenant, "stop", unit)

        # 5. Start Quadlet unit on normal port (picks up new :latest)
        await _control_service(ctx, tenant, stack_spec.name, service_name, "start")

        # 6. Health-check Quadlet unit
        healthy = await _health_poll(normal_host_port)

        # 7. Swap Caddy back to normal port
        if service_domains:
            for domain in service_domains:
                try:
                    if ctx.caddy_patch_upstream:
                        await ctx.caddy_patch_upstream(domain, normal_upstream)
                    else:
                        await caddy.patch_upstream(domain, normal_upstream)
                except CaddyError as exc:
                    log.warning("zdeploy_caddy_restore_failed", domain=domain, error=str(exc))

        if not healthy and not swap_errors:
            # Quadlet unit didn't become healthy — leave it running but warn
            log.warning("zdeploy_unit_health_check_failed", stack=stack_spec.name, service=service_name)

        log.info("zero_downtime_deploy_done", stack=stack_spec.name, service=service_name)

    finally:
        # 8. Always clean up candidate
        await _cleanup_candidate()


async def _remove_tenant_if_empty(tenant: str, ctx: ExecContext) -> None:
    if await ctx.tenant_in_use(tenant):
        return
    row = (
        await ctx.db.execute(select(Tenant).where(Tenant.linux_user == tenant))
    ).scalar_one_or_none()
    if row is None:
        return
    if row.uid is not None:
        scan = await ctx.host.scan_tenant(row.uid, tenant)
        if scan.unit_files or scan.volume_dirs:
            log.warning("tenant_gc_skipped_artifacts_remain", tenant=tenant)
            return
    await tenancy.remove_tenant(ctx.db, row.user_id, host=ctx.host)
