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
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Operation
from app.domain.actions import Action
from app.domain.planner import plan
from app.domain.specs import Observed, ObservedStack, StackSpec
from app.orchestration import executor, observer, operations
from app.services import notifications

log = structlog.get_logger("hosty.reconciler")

DesiredLoader = Callable[[AsyncSession], Awaitable[list[StackSpec]]]
ObserveFn = Callable[[AsyncSession], Awaitable[Observed]]
CaddySync = Callable[[AsyncSession], Awaitable[None]]
StatusHook = Callable[[AsyncSession, str, str, str | None], Awaitable[None]]


async def _no_desired(db: AsyncSession) -> list[StackSpec]:
    return []  # M4 replaces this with the DB-backed loader


async def _no_caddy(db: AsyncSession) -> None:  # pragma: no cover - default for M3 wiring
    return None


async def _no_status(db: AsyncSession, stack: str, status: str, error: str | None) -> None:
    return None  # M4 projects status onto the stacks table


@dataclass(frozen=True)
class StackOutcome:
    stack: str
    planned: int  # actions planned (0 = converged)
    executed: int
    error: str | None = None

    @property
    def converged(self) -> bool:
        return self.error is None


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

    def _lock(self, stack: str) -> asyncio.Lock:
        return self._locks.setdefault(stack, asyncio.Lock())

    # --- entrypoints ----------------------------------------------------------------

    async def run_loop(self) -> None:
        """Interval mode: converge everything, sleep, repeat. Never raises."""
        while True:
            try:
                await self.converge_all()
            except Exception as exc:
                log.error("reconcile_cycle_failed", error=str(exc))
            await asyncio.sleep(max(5, self._settings.reconcile_interval_seconds))

    async def converge_all(self) -> list[StackOutcome]:
        async with self._sessionmaker() as db:
            desired = await self._load_desired(db)
            observed = await self._observe(db)
        desired_by_name = {spec.name: spec for spec in desired}
        names = sorted(set(desired_by_name) | set(observed))
        tenants_in_use = {spec.tenant for spec in desired}
        outcomes = list(
            await asyncio.gather(
                *(
                    self._converge_one(
                        name, desired_by_name.get(name), observed.get(name), tenants_in_use
                    )
                    for name in names
                )
            )
        )
        await self._track_drift(outcomes)
        return outcomes

    async def converge_stack(self, name: str, *, operation_id: int | None = None) -> StackOutcome:
        """On-demand convergence after an API write (202 + operation)."""
        async with self._sessionmaker() as db:
            desired = await self._load_desired(db)
            observed = await self._observe(db)
        desired_by_name = {spec.name: spec for spec in desired}
        outcome = await self._converge_one(
            name,
            desired_by_name.get(name),
            observed.get(name),
            {spec.tenant for spec in desired},
            operation_id=operation_id,
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
    ) -> StackOutcome:
        async with self._semaphore, self._lock(name):
            actions = plan([spec] if spec else [], {name: obs} if obs is not None else {})
            if not actions:
                async with self._sessionmaker() as db:
                    await self._finish_operation(db, operation_id, actions, error=None)
                    # spec=None converged means the host holds nothing for
                    # this stack — "absent" lets the status hook finalize a
                    # pending deletion (drop the rows).
                    status = (
                        "absent" if spec is None else ("suspended" if spec.suspended else "ready")
                    )
                    await self._on_stack_status(db, name, status, None)
                return StackOutcome(stack=name, planned=0, executed=0)
            return await self._apply(name, spec, actions, tenants_in_use, operation_id)

    async def _apply(
        self,
        name: str,
        spec: StackSpec | None,
        actions: list[Action],
        tenants_in_use: set[str],
        operation_id: int | None,
    ) -> StackOutcome:
        async with self._sessionmaker() as db:
            op = await db.get(Operation, operation_id) if operation_id is not None else None
            if op is not None:
                op.status = "running"
                op.steps_json = operations.initial_steps_json(actions)
                await db.commit()

            async def tenant_in_use(tenant: str) -> bool:
                return tenant in tenants_in_use

            async def sync_caddy() -> None:
                await self._sync_caddy(db)

            ctx = executor.ExecContext(
                db=db, settings=self._settings, sync_caddy=sync_caddy, tenant_in_use=tenant_in_use
            )
            executed = 0
            for action in actions:
                step_name, _ = operations.step_for(action)
                if op is not None:
                    await operations.set_step(db, op, step_name, "running")
                try:
                    await executor.execute(action, ctx)
                except Exception as exc:
                    error = f"{step_name}: {exc}"
                    log.error("stack_action_failed", stack=name, step=step_name, error=str(exc))
                    if op is not None:
                        await operations.set_step(db, op, step_name, "failed")
                        await operations.finish(db, op, status="failed", error=error[:500])
                    await self._on_stack_status(db, name, "degraded", error[:500])
                    return StackOutcome(
                        stack=name, planned=len(actions), executed=executed, error=error
                    )
                if op is not None:
                    await operations.set_step(db, op, step_name, "done")
                executed += 1

            if op is not None:
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
