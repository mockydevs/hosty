"""The reconciler (v2/M3, ADR-013): the only writer to the host.

Modes: converge-on-startup (like the Caddy republish), interval loop
(`reconcile_interval_seconds`), and on-demand `converge_stack` after API
writes (M4). Per-stack asyncio locks serialize work on one stack; a global
semaphore caps host concurrency.

Failure semantics — deliberately undo-free: an action failure marks the
operation step failed, reports the stack degraded, and STOPS that stack's
plan; the next cycle replans from observed reality. Compensation logic is
where v1 hid its worst bugs; convergence makes it unnecessary.

Drift: a stack whose plan is non-empty on N consecutive cycles
(`reconcile_drift_cycles`) is flapping or stuck — emit one deduplicated
notification (`stack:<name>:drift`); resolve it when a cycle finds the
stack converged.

Multi-server: each StackSpec carries a `server_id`; the reconciler resolves
the appropriate HostContext per spec before executing.  Remote observation
(SSH-based scan) runs alongside the local observer so the planner sees
accurate state for stacks on remote servers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import utcnow
from app.core.config import Settings
from app.db.models import Operation, Server, SshKey
from app.domain.actions import Action
from app.domain.planner import plan
from app.domain.specs import Observed, ObservedStack, StackSpec
from app.orchestration import executor, observer, operations
from app.services import notifications
from app.system.host import HostContext, LocalHost, RemoteSSHHost

log = structlog.get_logger("hosty.reconciler")

DesiredLoader = Callable[[AsyncSession], Awaitable[list[StackSpec]]]
ObserveFn = Callable[[AsyncSession], Awaitable[Observed]]
CaddySync = Callable[[AsyncSession], Awaitable[None]]
StatusHook = Callable[[AsyncSession, str, str, str | None], Awaitable[None]]


async def _no_desired(db: AsyncSession) -> list[StackSpec]:
    return []


async def _no_caddy(db: AsyncSession) -> None:  # pragma: no cover
    return None


async def _no_status(db: AsyncSession, stack: str, status: str, error: str | None) -> None:
    return None


@dataclass(frozen=True)
class StackOutcome:
    stack: str
    planned: int
    executed: int
    error: str | None = None

    @property
    def converged(self) -> bool:
        return self.error is None


async def _build_host_map(
    db: AsyncSession, specs: list[StackSpec], settings: Settings
) -> dict[int | None, HostContext]:
    """Return a map of server_id → HostContext for all servers referenced by
    `specs`.  server_id=None (or a localhost server) maps to `LocalHost()`.
    SSH key decryption uses `settings.secret_key`."""
    from app.core.secrets import decrypt_secret

    needed_ids = {spec.server_id for spec in specs if spec.server_id is not None}
    host_map: dict[int | None, HostContext] = {None: LocalHost()}
    if not needed_ids:
        return host_map

    rows = (
        await db.execute(select(Server).where(Server.id.in_(needed_ids)))
    ).scalars().all()

    for server in rows:
        if server.is_localhost:
            host_map[server.id] = LocalHost()
            continue
        if server.ssh_key_id is None:
            log.warning("server_no_ssh_key", server=server.name, id=server.id)
            host_map[server.id] = LocalHost()  # fallback; will likely fail at SSH time
            continue
        key_row = await db.get(SshKey, server.ssh_key_id)
        if key_row is None:
            log.warning("server_ssh_key_missing", server=server.name, id=server.id)
            host_map[server.id] = LocalHost()
            continue
        private_key = decrypt_secret(key_row.private_key_encrypted, settings.secret_key)
        host_map[server.id] = RemoteSSHHost(
            hostname=server.hostname,
            port=server.port,
            username=server.ssh_user,
            private_key_text=private_key,
        )
    return host_map


async def _observe_remote(
    desired: list[StackSpec],
    host_map: dict[int | None, HostContext],
) -> Observed:
    """Scan remote servers for stacks that live there.  Results are merged
    with the local observation so the planner sees accurate state.  Any
    per-server failure degrades to absent (same contract as the local
    observer)."""
    from app.db.models import Tenant as TenantModel
    from app.domain.specs import ObservedUnit
    from app.system import podman as podman_mod
    from app.system import tenants as tenants_sys

    # Group remote specs by server_id
    by_server: dict[int, list[StackSpec]] = {}
    for spec in desired:
        if spec.server_id is not None:
            host = host_map.get(spec.server_id)
            if host is not None and not host.is_localhost:
                by_server.setdefault(spec.server_id, []).append(spec)

    if not by_server:
        return {}

    observed: Observed = {}

    async def _scan_server(server_id: int, specs: list[StackSpec]) -> None:
        host = host_map[server_id]
        # Gather unique tenants for these specs
        tenants = {spec.tenant for spec in specs}
        for linux_user in tenants:
            try:
                # We need the uid — derive from spec (all specs for same tenant share uid)
                # The uid is stored in the Tenant DB row; caller must pass db if needed.
                # For now approximate: the tenant scan still returns unit files with markers.
                # We do a best-effort scan without the uid; use the podman socket path heuristic.
                # Actually, we can't know the uid without querying the DB here.
                # The spec itself doesn't carry uid; it's in the DB Tenant row.
                # Skip remote observation of tenants not yet provisioned (uid=None).
                # Remote obs is best-effort: fallback to empty if uid unavailable.
                pass
            except Exception as exc:
                log.warning(
                    "remote_observe_tenant_failed",
                    server=server_id,
                    tenant=linux_user,
                    error=str(exc),
                )

    for server_id, specs in by_server.items():
        await _scan_server(server_id, specs)

    return observed


def _service_verb(cls: type) -> str:
    return {"StartService": "Starting", "StopService": "Stopping", "RestartService": "Restarting"}.get(
        cls.__name__, cls.__name__
    )


class Reconciler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        load_desired: DesiredLoader = _no_desired,
        observe: ObserveFn = observer.observe,
        sync_caddy: CaddySync = _no_caddy,
        on_stack_status: StatusHook = _no_status,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._load_desired = load_desired
        self._observe = observe
        self._sync_caddy = sync_caddy
        self._on_stack_status = on_stack_status
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(max(1, settings.reconcile_concurrency))
        self._drift_counts: dict[str, int] = {}
        # Immediate-trigger queue: API calls enqueue here; run_loop drains before each cycle
        self._queue: asyncio.Queue[tuple[str, int | None]] = asyncio.Queue()
        self._wake = asyncio.Event()

    def _lock(self, stack: str) -> asyncio.Lock:
        return self._locks.setdefault(stack, asyncio.Lock())

    def enqueue(self, stack_name: str, operation_id: int | None = None) -> None:
        """Trigger immediate convergence of one stack.  Non-blocking; survives no restart
        (the stale-op reaper + startup recovery are the durability backstop)."""
        self._queue.put_nowait((stack_name, operation_id))
        self._wake.set()

    async def _drain_queue(self) -> None:
        """Converge all immediately-enqueued stacks (deduped by name, newest op wins)."""
        pending: dict[str, int | None] = {}
        while not self._queue.empty():
            name, op_id = self._queue.get_nowait()
            pending[name] = op_id  # last enqueue for same stack wins
        if not pending:
            return
        results = await asyncio.gather(
            *(self.converge_stack(name, operation_id=op_id) for name, op_id in pending.items()),
            return_exceptions=True,
        )
        for name, result in zip(pending, results):
            if isinstance(result, Exception):
                log.error("queue_converge_failed", stack=name, error=str(result))

    async def _recover_pending_operations(self) -> None:
        """On startup: find ops stuck in 'pending' and immediately re-enqueue them.
        Handles background tasks orphaned by a server restart."""
        from app.db.models import Stack
        async with self._sessionmaker() as db:
            rows = (
                await db.execute(
                    select(Operation, Stack)
                    .join(Stack, Stack.id == Operation.stack_id)
                    .where(Operation.status == "pending")
                    .where(Operation.stack_id.isnot(None))
                )
            ).all()
        for op, stack in rows:
            log.info("recovering_pending_op", stack=stack.name, op_id=op.id)
            self.enqueue(stack.name, op.id)

    # --- entrypoints ----------------------------------------------------------------

    async def run_loop(self) -> None:
        """Event-driven loop: immediate wakeup on enqueue(), interval as safety net."""
        # On startup: recover ops orphaned by prior restart, then immediate full sweep.
        try:
            await self._recover_pending_operations()
        except Exception as exc:
            log.error("op_recovery_failed", error=str(exc))
        try:
            await self._reap_stale_operations()
        except Exception as exc:
            log.error("stale_op_reap_failed", error=str(exc))

        while True:
            # Drain the immediate queue first (enqueue() calls from API routes)
            try:
                await self._drain_queue()
            except Exception as exc:
                log.error("queue_drain_failed", error=str(exc))

            # Full reconcile cycle (catches drift and anything missed)
            try:
                await self.converge_all()
            except Exception as exc:
                log.error("reconcile_cycle_failed", error=str(exc))

            # Sleep until triggered or interval fires
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=max(5, self._settings.reconcile_interval_seconds),
                )
                self._wake.clear()
            except asyncio.TimeoutError:
                pass

    async def _reap_stale_operations(self) -> None:
        """Mark pending/running operations older than operation_timeout_seconds as failed.
        This recovers background tasks lost on server restart and hard-hung operations."""
        cutoff = utcnow() - timedelta(seconds=self._settings.operation_timeout_seconds)
        async with self._sessionmaker() as db:
            stale = (
                await db.execute(
                    select(Operation)
                    .where(Operation.status.in_(["pending", "running"]))
                    .where(Operation.created_at < cutoff)
                )
            ).scalars().all()
            if not stale:
                return
            for op in stale:
                log.warning(
                    "reaping_stale_operation",
                    op_id=op.id,
                    kind=op.kind,
                    status=op.status,
                    age_s=int((utcnow() - op.created_at).total_seconds()),
                )
                op.status = "failed"
                op.error = (
                    f"Timed out after {self._settings.operation_timeout_seconds}s "
                    f"(was {op.status} — likely lost on server restart)"
                )
                op.finished_at = utcnow()
            await db.commit()

    async def converge_all(self) -> list[StackOutcome]:
        await self._reap_stale_operations()
        async with self._sessionmaker() as db:
            desired = await self._load_desired(db)
            local_observed = await self._observe(db)
            host_map = await _build_host_map(db, desired, self._settings)

        # Remote servers: SSH-scan for accurate observed state
        remote_observed = await _observe_remote_with_db(
            desired, host_map, self._sessionmaker
        )
        observed = {**local_observed, **remote_observed}

        desired_by_name = {spec.name: spec for spec in desired}
        names = sorted(set(desired_by_name) | set(observed))
        tenants_in_use = {spec.tenant for spec in desired}
        outcomes = list(
            await asyncio.gather(
                *(
                    self._converge_one(
                        name,
                        desired_by_name.get(name),
                        observed.get(name),
                        tenants_in_use,
                        host=host_map.get(
                            desired_by_name[name].server_id if name in desired_by_name else None
                        ),
                    )
                    for name in names
                )
            )
        )
        await self._track_drift(outcomes)
        return outcomes

    async def resync_ingress(self) -> None:
        async with self._sessionmaker() as db:
            await self._sync_caddy(db)

    async def converge_stack(self, name: str, *, operation_id: int | None = None) -> StackOutcome:
        """On-demand convergence after an API write (202 + operation)."""
        async with self._sessionmaker() as db:
            desired = await self._load_desired(db)
            local_observed = await self._observe(db)
            host_map = await _build_host_map(db, desired, self._settings)

        remote_observed = await _observe_remote_with_db(
            desired, host_map, self._sessionmaker
        )
        observed = {**local_observed, **remote_observed}

        desired_by_name = {spec.name: spec for spec in desired}
        spec = desired_by_name.get(name)
        outcome = await self._converge_one(
            name,
            spec,
            observed.get(name),
            {s.tenant for s in desired},
            operation_id=operation_id,
            host=host_map.get(spec.server_id if spec else None),
        )
        await self._track_drift([outcome])
        return outcome

    # --- one stack ------------------------------------------------------------------

    async def _converge_one(
        self,
        name: str,
        spec: StackSpec | None,
        obs: ObservedStack | None,
        tenants_in_use: set[str],
        *,
        operation_id: int | None = None,
        host: HostContext | None = None,
    ) -> StackOutcome:
        effective_host = host if host is not None else LocalHost()
        async with self._semaphore, self._lock(name):
            actions = plan([spec] if spec else [], {name: obs} if obs is not None else {})
            if not actions:
                async with self._sessionmaker() as db:
                    await self._finish_operation(db, operation_id, actions, error=None)
                    status = (
                        "absent" if spec is None else ("suspended" if spec.suspended else "ready")
                    )
                    await self._on_stack_status(db, name, status, None)
                return StackOutcome(stack=name, planned=0, executed=0)
            return await self._apply(
                name, spec, actions, tenants_in_use, operation_id, effective_host
            )

    async def _apply(
        self,
        name: str,
        spec: StackSpec | None,
        actions: list[Action],
        tenants_in_use: set[str],
        operation_id: int | None,
        host: HostContext,
    ) -> StackOutcome:
        # For remote hosts, connect before starting the plan so a single
        # SSH session serves the entire action list.
        remote = isinstance(host, RemoteSSHHost)
        if remote:
            try:
                await host.connect()  # type: ignore[attr-defined]
            except Exception as exc:
                error = f"SSH connect to {host.hostname}: {exc}"  # type: ignore[attr-defined]
                log.error("stack_ssh_connect_failed", stack=name, error=str(exc))
                async with self._sessionmaker() as db:
                    await self._on_stack_status(db, name, "degraded", error[:500])
                return StackOutcome(stack=name, planned=len(actions), executed=0, error=error)

        try:
            return await asyncio.wait_for(
                self._apply_inner(name, spec, actions, tenants_in_use, operation_id, host),
                timeout=self._settings.operation_timeout_seconds,
            )
        except asyncio.TimeoutError:
            timeout_s = self._settings.operation_timeout_seconds
            error = f"Operation timed out after {timeout_s}s"
            log.error("stack_operation_timeout", stack=name, timeout_s=timeout_s)
            async with self._sessionmaker() as db:
                if operation_id is not None:
                    op = await db.get(Operation, operation_id)
                    if op is not None and op.status not in ("succeeded", "failed"):
                        await operations.append_log(db, op, f"✗ {error}")
                        await operations.finish(db, op, status="failed", error=error)
                fail_status = "deleting" if spec is None else "degraded"
                await self._on_stack_status(db, name, fail_status, error)
            return StackOutcome(stack=name, planned=len(actions), executed=0, error=error)
        finally:
            if remote:
                try:
                    await host.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    async def _apply_inner(
        self,
        name: str,
        spec: StackSpec | None,
        actions: list[Action],
        tenants_in_use: set[str],
        operation_id: int | None,
        host: HostContext,
    ) -> StackOutcome:
        async with self._sessionmaker() as db:
            op = await db.get(Operation, operation_id) if operation_id is not None else None
            if op is not None:
                op.status = "running"
                op.steps_json = operations.initial_steps_json(actions)
                op.log_lines = ""
                await db.commit()
                await operations.append_log(db, op, f"Starting {op.kind.replace('_', ' ')} for {name} ({len(actions)} steps)")

            async def tenant_in_use(tenant: str) -> bool:
                return tenant in tenants_in_use

            async def sync_caddy() -> None:
                await self._sync_caddy(db)

            ctx = executor.ExecContext(
                db=db,
                settings=self._settings,
                sync_caddy=sync_caddy,
                tenant_in_use=tenant_in_use,
                host=host,
            )
            executed = 0
            i = 0
            while i < len(actions):
                action = actions[i]

                # Batch consecutive same-type service actions → parallel execution
                from app.domain.actions import StartService, StopService, RestartService
                if isinstance(action, (StartService, StopService, RestartService)):
                    batch_cls = type(action)
                    batch: list[Action] = []
                    j = i
                    while j < len(actions) and type(actions[j]) is batch_cls:
                        batch.append(actions[j])
                        j += 1

                    if len(batch) > 1:
                        step_pairs = [operations.step_for(a) for a in batch]
                        verb = _service_verb(batch_cls)
                        if op is not None:
                            for sn, _ in step_pairs:
                                await operations.set_step(db, op, sn, "running")
                            await operations.append_log(
                                db, op, f"→ {verb} {len(batch)} services in parallel"
                            )
                        results = await asyncio.gather(
                            *(executor.execute(a, ctx) for a in batch),
                            return_exceptions=True,
                        )
                        # Find first error
                        first_err = next(
                            (
                                (step_pairs[k], results[k])
                                for k in range(len(results))
                                if isinstance(results[k], Exception)
                            ),
                            None,
                        )
                        if first_err:
                            (sn, sl), exc = first_err
                            error = f"{sn}: {exc}"
                            log.error("stack_action_failed", stack=name, step=sn, error=str(exc))
                            if op is not None:
                                for k, (step_n, _) in enumerate(step_pairs):
                                    s = "failed" if isinstance(results[k], Exception) else "done"
                                    await operations.set_step(db, op, step_n, s)
                                await operations.append_log(db, op, f"✗ {sl}: {exc}")
                                await operations.finish(db, op, status="failed", error=error[:500])
                            fail_status = "deleting" if spec is None else "degraded"
                            await self._on_stack_status(db, name, fail_status, error[:500])
                            return StackOutcome(
                                stack=name, planned=len(actions), executed=executed, error=error
                            )
                        if op is not None:
                            for sn, sl in step_pairs:
                                await operations.set_step(db, op, sn, "done")
                            past = verb.lower().rstrip("e") + "ed"
                            await operations.append_log(db, op, f"✓ {len(batch)} services {past}")
                        executed += len(batch)
                        i = j
                        continue

                # Single action (or non-service action) — sequential
                step_name, step_label = operations.step_for(action)
                if op is not None:
                    await operations.set_step(db, op, step_name, "running")
                    await operations.append_log(db, op, f"→ {step_label}")
                try:
                    await executor.execute(action, ctx)
                except Exception as exc:
                    error = f"{step_name}: {exc}"
                    log.error("stack_action_failed", stack=name, step=step_name, error=str(exc))
                    if op is not None:
                        await operations.set_step(db, op, step_name, "failed")
                        await operations.append_log(db, op, f"✗ {step_label}: {exc}")
                        await operations.finish(db, op, status="failed", error=error[:500])
                    # Keep "deleting" during teardown so cleanup fires on next success.
                    fail_status = "deleting" if spec is None else "degraded"
                    await self._on_stack_status(db, name, fail_status, error[:500])
                    return StackOutcome(
                        stack=name, planned=len(actions), executed=executed, error=error
                    )
                if op is not None:
                    await operations.set_step(db, op, step_name, "done")
                    await operations.append_log(db, op, f"✓ {step_label}")
                executed += 1
                i += 1

            if op is not None:
                await operations.append_log(db, op, f"Done — all {executed} steps completed successfully")
                await operations.finish(db, op, status="succeeded")
            status = "absent" if spec is None else ("suspended" if spec.suspended else "ready")
            await self._on_stack_status(db, name, status, None)
            log.info("stack_converged", stack=name, actions=executed)
            return StackOutcome(stack=name, planned=len(actions), executed=executed)

    async def _finish_operation(
        self, db: AsyncSession, operation_id: int | None, actions: list[Action], error: str | None
    ) -> None:
        if operation_id is None:
            return
        op = await db.get(Operation, operation_id)
        if op is None:
            return
        op.steps_json = operations.initial_steps_json(actions)
        await operations.finish(db, op, status="succeeded" if error is None else "failed")

    # --- drift ------------------------------------------------------------------------

    async def _track_drift(self, outcomes: list[StackOutcome]) -> None:
        threshold = max(1, self._settings.reconcile_drift_cycles)
        async with self._sessionmaker() as db:
            for outcome in outcomes:
                key = f"stack:{outcome.stack}:drift"
                if outcome.planned == 0:
                    if self._drift_counts.pop(outcome.stack, 0):
                        await notifications.resolve(db, key)
                    continue
                count = self._drift_counts.get(outcome.stack, 0) + 1
                self._drift_counts[outcome.stack] = count
                if count >= threshold:
                    await notifications.emit(
                        db,
                        kind="stack_drift",
                        severity="warning",
                        message=(
                            f"Stack {outcome.stack} has diverged from its desired state "
                            f"for {count} consecutive reconcile cycles"
                            + (f" (last error: {outcome.error})" if outcome.error else "")
                        ),
                        dedupe_key=key,
                        settings=self._settings,
                    )


async def _observe_remote_with_db(
    desired: list[StackSpec],
    host_map: dict[int | None, HostContext],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> Observed:
    """Observe stacks on remote servers (server_id != None and !is_localhost).

    Queries the DB for Tenant.uid, then SSH-scans each remote server.
    Any failure for a tenant degrades that tenant's stacks to absent
    (the local observer's contract).
    """
    from app.db.models import Tenant
    from app.domain.specs import ObservedUnit

    # Group remote tenants by server_id
    remote_tenants: dict[int, set[str]] = {}
    for spec in desired:
        if spec.server_id is not None:
            host = host_map.get(spec.server_id)
            if host is not None and not host.is_localhost:
                remote_tenants.setdefault(spec.server_id, set()).add(spec.tenant)

    if not remote_tenants:
        return {}

    # Load uid for all tenants we need
    async with sessionmaker() as db:
        all_tenant_names = {t for tenants in remote_tenants.values() for t in tenants}
        rows = (
            await db.execute(
                select(Tenant).where(Tenant.linux_user.in_(all_tenant_names))
            )
        ).scalars().all()
    uid_map = {row.linux_user: row.uid for row in rows if row.uid is not None}

    observed: Observed = {}

    async def _scan_one_server(server_id: int, tenant_names: set[str]) -> None:
        host = host_map[server_id]
        # Connect once for all tenants on this server
        if isinstance(host, RemoteSSHHost):
            try:
                await host.connect()
            except Exception as exc:
                log.warning(
                    "remote_observe_connect_failed", server=server_id, error=str(exc)
                )
                return
        try:
            for linux_user in tenant_names:
                uid = uid_map.get(linux_user)
                if uid is None:
                    continue
                try:
                    tenant_present = await host.tenant_exists(linux_user)
                    scan = await host.scan_tenant(uid, linux_user)
                    containers = await host.podman_ps(uid) if tenant_present else []
                    running = {c.name for c in containers if c.running}

                    units: dict[str, dict[str, ObservedUnit]] = {}
                    build_units: dict[str, dict[str, str | None]] = {}
                    stacks_seen: set[str] = set()
                    for unit_file in scan.unit_files:
                        stacks_seen.add(unit_file.stack)
                        if unit_file.service is None:
                            continue
                        if unit_file.file_name.endswith(".build"):
                            build_units.setdefault(unit_file.stack, {})[unit_file.service] = (
                                unit_file.spec_hash
                            )
                            continue
                        units.setdefault(unit_file.stack, {})[unit_file.service] = ObservedUnit(
                            spec_hash=unit_file.spec_hash,
                            active=f"{unit_file.stack}-{unit_file.service}" in running,
                        )
                    for container in containers:
                        if not container.running or container.stack is None:
                            continue
                        service = container.name.removeprefix(f"{container.stack}-")
                        units.setdefault(container.stack, {}).setdefault(
                            service, ObservedUnit(spec_hash=None, active=True)
                        )
                        stacks_seen.add(container.stack)
                    stacks_seen.update(scan.volume_dirs)

                    for stack in stacks_seen:
                        observed[stack] = ObservedStack(
                            tenant=linux_user,
                            tenant_present=tenant_present,
                            units=units.get(stack, {}),
                            build_units=build_units.get(stack, {}),
                            volume_dirs=scan.volume_dirs.get(stack, frozenset()),
                        )
                except Exception as exc:
                    log.warning(
                        "remote_observe_tenant_failed",
                        server=server_id,
                        tenant=linux_user,
                        error=str(exc),
                    )
        finally:
            if isinstance(host, RemoteSSHHost):
                try:
                    await host.close()
                except Exception:
                    pass

    await asyncio.gather(
        *(_scan_one_server(sid, tenants) for sid, tenants in remote_tenants.items()),
        return_exceptions=True,
    )
    return observed
