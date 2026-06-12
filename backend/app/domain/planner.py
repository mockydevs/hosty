"""The pure planner (v2/M1, ADR-013): `plan(desired, observed) -> [Action]`.

The two theorems the whole system rests on (property-tested in
tests/test_domain_planner.py):

  convergence:  applying the plan to any observed host yields the desired
                state — `observe(apply(plan(d, o), host)) == project(d)`
  idempotency:  a converged host plans nothing — `plan(d, project(d)) == []`

Semantics:
- A stack present in `observed` but absent from `desired` is torn down
  (stop → remove units → reload → remove volume dirs → tenant GC). A stack
  whose observed artifacts belong to a DIFFERENT tenant than desired is
  torn down the same way, then created fresh.
- A desired stack is converged: tenant ensured, volume dirs ensured, unit
  files synced when any is missing/stale/extra (spec_hash mismatch),
  services started (or stopped when the stack is suspended), orphaned
  volume dirs removed.
- Suspension is scale-to-zero: units stay written (resume is instant),
  services stopped, volumes kept. No special mechanism anywhere else.
- One trailing SyncCaddy if and only if the plan is otherwise non-empty:
  ingress is derived state, rebuilt whenever anything else moved, and a
  converged host MUST plan [] (so Caddy state never feeds back as input).

Ordering rules the executor relies on:
- deletions before creations (frees names/ports first),
- per stack: EnsureTenant → EnsureVolumeDir* → StopService* (extra/suspend)
  → WriteUnits → DaemonReload → Start/RestartService* → RemoveVolumeDir*,
- deterministic: stacks and members in sorted order.
"""

from __future__ import annotations

from app.domain.actions import (
    Action,
    DaemonReload,
    EnsureTenant,
    EnsureVolumeDir,
    RemoveTenantIfEmpty,
    RemoveUnits,
    RemoveVolumeDir,
    RestartService,
    StartService,
    StopService,
    SyncCaddy,
    WriteUnits,
)
from app.domain.specs import Observed, ObservedStack, StackSpec, spec_hash


def plan(desired: list[StackSpec], observed: Observed) -> list[Action]:
    desired_by_name = {spec.name: spec for spec in desired}
    if len(desired_by_name) != len(desired):
        raise ValueError("Duplicate stack names in desired state")

    actions: list[Action] = []
    for name in sorted(set(observed) - set(desired_by_name)):
        actions.extend(_plan_teardown(name, observed[name]))
    for name in sorted(desired_by_name):
        spec = desired_by_name[name]
        obs = observed.get(name)
        if obs is not None and obs.tenant != spec.tenant:
            # The host artifacts belong to a different tenant (unit files
            # live under the OLD uid's directory): tear down, rebuild fresh.
            actions.extend(_plan_teardown(name, obs))
            obs = None
        actions.extend(_plan_converge(spec, obs))
    if actions:
        actions.append(SyncCaddy())
    return actions


def _plan_teardown(name: str, obs: ObservedStack) -> list[Action]:
    actions: list[Action] = []
    for service in sorted(obs.units):
        if obs.units[service].active:
            actions.append(StopService(obs.tenant, name, service))
    # Unconditional: the observer reports a stack on ANY artifact, and unit
    # files it cannot model per-service (the .network file) may remain even
    # when `units` is empty. RemoveUnits is an idempotent sync-to-nothing.
    actions.append(RemoveUnits(obs.tenant, name))
    actions.append(DaemonReload(obs.tenant))
    for volume in sorted(obs.volume_dirs):
        actions.append(RemoveVolumeDir(obs.tenant, name, volume))
    actions.append(RemoveTenantIfEmpty(obs.tenant))
    return actions


def _plan_converge(spec: StackSpec, obs: ObservedStack | None) -> list[Action]:
    actions: list[Action] = []
    observed_units = obs.units if obs else {}
    observed_dirs = obs.volume_dirs if obs else frozenset()

    if obs is None or not obs.tenant_present:
        actions.append(EnsureTenant(spec.tenant))

    desired_dirs = {volume.name for volume in spec.volumes}
    for volume in sorted(desired_dirs - observed_dirs):
        actions.append(EnsureVolumeDir(spec.tenant, spec.name, volume))

    expected = {service.name: spec_hash(spec, service) for service in spec.services}
    stale = {
        name
        for name, hashed in expected.items()
        if name not in observed_units or observed_units[name].spec_hash != hashed
    }
    extra = set(observed_units) - set(expected)

    for service in sorted(extra):
        if observed_units[service].active:
            actions.append(StopService(spec.tenant, spec.name, service))
    rewrite = bool(stale or extra)
    if rewrite:
        actions.append(WriteUnits(spec))
        actions.append(DaemonReload(spec.tenant))

    for service in sorted(expected):
        unit = observed_units.get(service)
        active = unit.active if unit else False
        if spec.suspended:
            if active:
                actions.append(StopService(spec.tenant, spec.name, service))
        elif active and service in stale:
            actions.append(RestartService(spec.tenant, spec.name, service))
        elif not active:
            actions.append(StartService(spec.tenant, spec.name, service))

    for volume in sorted(observed_dirs - desired_dirs):
        actions.append(RemoveVolumeDir(spec.tenant, spec.name, volume))
    return actions
