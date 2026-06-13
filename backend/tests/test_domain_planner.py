"""The planner (v2/M1): exhaustive lifecycle scenarios plus the two
property-based theorems the system rests on —

  convergence:  observe(apply(plan(desired, observed))) == project(desired)
  idempotency:  plan(desired, project(desired)) == []

The ModelHost interprets actions the way the real adapters will (M2/M3):
unit files per stack, an active set (systemd keeps a running unit alive
even after its file is deleted, until stopped), volume dirs, tenants.
Create-side actions assert their preconditions so ordering bugs in the
planner fail loudly under hypothesis."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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
from app.domain.planner import plan
from app.domain.specs import (
    Observed,
    ObservedStack,
    ObservedUnit,
    ServiceSpec,
    StackSpec,
    VolumeSpec,
    spec_hash,
)

# --- model host --------------------------------------------------------------------


@dataclass
class _StackState:
    tenant: str
    files: dict[str, str | None] = field(default_factory=dict)  # service -> spec hash
    builds: dict[str, str | None] = field(default_factory=dict)  # service -> spec hash
    active: set[str] = field(default_factory=set)
    dirs: set[str] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not (self.files or self.builds or self.active or self.dirs)


class ModelHost:
    def __init__(self) -> None:
        self.tenants: set[str] = set()
        self.stacks: dict[str, _StackState] = {}

    def seed_converged(self, spec: StackSpec) -> None:
        """Make this host look exactly converged with `spec`."""
        self.tenants.add(spec.tenant)
        state = _StackState(tenant=spec.tenant)
        state.files = {svc.name: spec_hash(spec, svc) for svc in spec.services}
        state.builds = {
            svc.name: spec_hash(spec, svc) for svc in spec.services if svc.build_repo
        }
        if not spec.suspended:
            state.active = set(state.files)
        state.dirs = {vol.name for vol in spec.volumes}
        self.stacks[spec.name] = state

    def _entry(self, stack: str, tenant: str) -> _StackState:
        state = self.stacks.get(stack)
        if state is None:
            state = self.stacks[stack] = _StackState(tenant=tenant)
        return state

    def apply(self, actions: list[Action]) -> None:
        for action in actions:
            self._apply_one(action)
        self.stacks = {name: st for name, st in self.stacks.items() if not st.empty}

    def _apply_one(self, action: Action) -> None:
        match action:
            case EnsureTenant(tenant):
                self.tenants.add(tenant)
            case EnsureVolumeDir(tenant, stack, volume):
                assert tenant in self.tenants, "EnsureVolumeDir before EnsureTenant"
                self._entry(stack, tenant).dirs.add(volume)
            case WriteUnits(spec):
                assert spec.tenant in self.tenants, "WriteUnits before EnsureTenant"
                state = self._entry(spec.name, spec.tenant)
                state.tenant = spec.tenant
                state.files = {svc.name: spec_hash(spec, svc) for svc in spec.services}
                state.builds = {
                    svc.name: spec_hash(spec, svc) for svc in spec.services if svc.build_repo
                }
            case RemoveUnits(_, stack):
                if stack in self.stacks:
                    self.stacks[stack].files = {}
                    self.stacks[stack].builds = {}
            case StartService(tenant, stack, service) | RestartService(tenant, stack, service):
                assert tenant in self.tenants, "start before EnsureTenant"
                state = self.stacks.get(stack)
                assert state is not None and service in state.files, "start without unit file"
                state.active.add(service)
            case StopService(_, stack, service):
                if stack in self.stacks:
                    self.stacks[stack].active.discard(service)
            case RemoveVolumeDir(_, stack, volume):
                if stack in self.stacks:
                    self.stacks[stack].dirs.discard(volume)
            case RemoveTenantIfEmpty(tenant):
                if not any(s.tenant == tenant and not s.empty for s in self.stacks.values()):
                    self.tenants.discard(tenant)
            case DaemonReload() | SyncCaddy():
                pass

    def observe(self) -> Observed:
        out: Observed = {}
        for name, state in self.stacks.items():
            if state.empty:
                continue
            units = {
                svc: ObservedUnit(spec_hash=state.files.get(svc), active=svc in state.active)
                for svc in sorted(set(state.files) | state.active)
            }
            out[name] = ObservedStack(
                tenant=state.tenant,
                tenant_present=state.tenant in self.tenants,
                units=units,
                build_units=dict(state.builds),
                volume_dirs=frozenset(state.dirs),
            )
        return out


def project(desired: list[StackSpec]) -> Observed:
    """What a perfectly converged host observes."""
    return {
        spec.name: ObservedStack(
            tenant=spec.tenant,
            tenant_present=True,
            units={
                svc.name: ObservedUnit(spec_hash(spec, svc), active=not spec.suspended)
                for svc in spec.services
            },
            build_units={
                svc.name: spec_hash(spec, svc) for svc in spec.services if svc.build_repo
            },
            volume_dirs=frozenset(vol.name for vol in spec.volumes),
        )
        for spec in desired
    }


# --- spec builders -----------------------------------------------------------------


def web(**overrides) -> ServiceSpec:
    base = dict(name="web", image="nginx:1.27", internal_port=80, is_web=True)
    base.update(overrides)
    return ServiceSpec(**base)


def stack(name: str = "blog", **overrides) -> StackSpec:
    base = dict(name=name, tenant="hosty-t-7", loopback_ip="127.1.0.1", services=(web(),))
    base.update(overrides)
    return StackSpec(**base)


def converged(*specs: StackSpec) -> Observed:
    return project(list(specs))


# --- scenario tests ----------------------------------------------------------------


def test_empty_world_plans_nothing():
    assert plan([], {}) == []


def test_converged_stack_plans_nothing():
    spec = stack(volumes=(VolumeSpec(name="data", service="web", mount_path="/data"),))
    assert plan([spec], converged(spec)) == []


def test_converged_suspended_stack_plans_nothing():
    spec = stack(suspended=True)
    assert plan([spec], converged(spec)) == []


def test_fresh_create_emits_full_ordered_sequence():
    spec = stack(volumes=(VolumeSpec(name="data", service="web", mount_path="/data"),))
    assert plan([spec], {}) == [
        EnsureTenant("hosty-t-7"),
        EnsureVolumeDir("hosty-t-7", "blog", "data"),
        WriteUnits(spec),
        DaemonReload("hosty-t-7"),
        StartService("hosty-t-7", "blog", "web"),
        SyncCaddy(),
    ]


def test_fresh_create_of_suspended_stack_never_starts():
    spec = stack(suspended=True)
    actions = plan([spec], {})
    assert StartService("hosty-t-7", "blog", "web") not in actions
    assert WriteUnits(spec) in actions


def test_delete_emits_full_ordered_sequence():
    spec = stack(volumes=(VolumeSpec(name="data", service="web", mount_path="/data"),))
    assert plan([], converged(spec)) == [
        StopService("hosty-t-7", "blog", "web"),
        RemoveUnits("hosty-t-7", "blog"),
        DaemonReload("hosty-t-7"),
        RemoveVolumeDir("hosty-t-7", "blog", "data"),
        RemoveTenantIfEmpty("hosty-t-7"),
        SyncCaddy(),
    ]


def test_delete_of_stopped_remnants_skips_stop_but_still_syncs_units():
    # A `.network` file may remain even when no service units are observed,
    # so teardown always syncs the unit dir to nothing.
    observed = {
        "blog": ObservedStack(tenant="hosty-t-7", units={}, volume_dirs=frozenset({"data"}))
    }
    assert plan([], observed) == [
        RemoveUnits("hosty-t-7", "blog"),
        DaemonReload("hosty-t-7"),
        RemoveVolumeDir("hosty-t-7", "blog", "data"),
        RemoveTenantIfEmpty("hosty-t-7"),
        SyncCaddy(),
    ]


@pytest.mark.parametrize(
    "changed",
    [
        stack(services=(web(image="nginx:1.28"),)),  # image change
        stack(services=(web(env=(("KEY", "v"),)),)),  # env change
        stack(services=(web(internal_port=8080),)),  # port change
    ],
)
def test_spec_change_rewrites_and_restarts(changed):
    old = stack()
    assert plan([changed], converged(old)) == [
        WriteUnits(changed),
        DaemonReload("hosty-t-7"),
        RestartService("hosty-t-7", "blog", "web"),
        SyncCaddy(),
    ]


def test_suspension_stops_keeps_units_and_volumes():
    running = stack(volumes=(VolumeSpec(name="data", service="web", mount_path="/data"),))
    suspended = stack(
        suspended=True, volumes=(VolumeSpec(name="data", service="web", mount_path="/data"),)
    )
    assert plan([suspended], converged(running)) == [
        StopService("hosty-t-7", "blog", "web"),
        SyncCaddy(),
    ]


def test_resume_starts_without_rewrite():
    suspended = stack(suspended=True)
    resumed = stack()
    assert plan([resumed], converged(suspended)) == [
        StartService("hosty-t-7", "blog", "web"),
        SyncCaddy(),
    ]


def test_drift_stopped_unit_restarts_without_rewrite():
    spec = stack()
    observed = {
        "blog": ObservedStack(
            tenant="hosty-t-7",
            units={"web": ObservedUnit(spec_hash(spec, spec.services[0]), active=False)},
        )
    }
    assert plan([spec], observed) == [
        StartService("hosty-t-7", "blog", "web"),
        SyncCaddy(),
    ]


def test_drift_missing_unit_file_rewrites():
    spec = stack()
    observed = {"blog": ObservedStack(tenant="hosty-t-7", units={})}
    actions = plan([spec], observed)
    assert WriteUnits(spec) in actions
    assert StartService("hosty-t-7", "blog", "web") in actions


def test_drift_missing_build_unit_rewrites_without_restarting_running_container():
    service = web(
        image="hosty-build-target",
        build_repo="https://github.com/example/app.git",
        build_branch="main",
    )
    spec = stack(services=(service,))
    observed = converged(spec)
    observed["blog"] = ObservedStack(
        tenant="hosty-t-7",
        units=observed["blog"].units,
        build_units={},
    )
    assert plan([spec], observed) == [
        WriteUnits(spec),
        DaemonReload("hosty-t-7"),
        SyncCaddy(),
    ]


def test_orphaned_build_unit_is_pruned():
    spec = stack()
    observed = converged(spec)
    observed["blog"] = ObservedStack(
        tenant="hosty-t-7",
        units=observed["blog"].units,
        build_units={"web": "old"},
    )
    assert plan([spec], observed) == [
        WriteUnits(spec),
        DaemonReload("hosty-t-7"),
        SyncCaddy(),
    ]


def test_drift_corrupt_marker_rewrites_and_restarts():
    spec = stack()
    observed = {
        "blog": ObservedStack(tenant="hosty-t-7", units={"web": ObservedUnit(None, active=True)})
    }
    assert plan([spec], observed) == [
        WriteUnits(spec),
        DaemonReload("hosty-t-7"),
        RestartService("hosty-t-7", "blog", "web"),
        SyncCaddy(),
    ]


def test_extra_service_is_stopped_and_pruned_by_rewrite():
    spec = stack()
    observed = converged(spec)
    units = dict(observed["blog"].units)
    units["ghost"] = ObservedUnit("whatever", active=True)
    observed["blog"] = ObservedStack(tenant="hosty-t-7", units=units)
    assert plan([spec], observed) == [
        StopService("hosty-t-7", "blog", "ghost"),
        WriteUnits(spec),
        DaemonReload("hosty-t-7"),
        SyncCaddy(),
    ]


def test_extra_volume_dir_is_removed():
    spec = stack()
    observed = converged(spec)
    observed["blog"] = ObservedStack(
        tenant="hosty-t-7", units=observed["blog"].units, volume_dirs=frozenset({"old"})
    )
    assert plan([spec], observed) == [
        RemoveVolumeDir("hosty-t-7", "blog", "old"),
        SyncCaddy(),
    ]


def test_missing_tenant_is_reensured():
    spec = stack()
    observed = converged(spec)
    observed["blog"] = ObservedStack(
        tenant="hosty-t-7", tenant_present=False, units=observed["blog"].units
    )
    assert plan([spec], observed) == [EnsureTenant("hosty-t-7"), SyncCaddy()]


def test_tenant_mismatch_tears_down_then_recreates():
    spec = stack()  # tenant hosty-t-7
    foreign = stack(tenant="hosty-t-8")
    actions = plan([spec], converged(foreign))
    assert actions == [
        StopService("hosty-t-8", "blog", "web"),
        RemoveUnits("hosty-t-8", "blog"),
        DaemonReload("hosty-t-8"),
        RemoveTenantIfEmpty("hosty-t-8"),
        EnsureTenant("hosty-t-7"),
        WriteUnits(spec),
        DaemonReload("hosty-t-7"),
        StartService("hosty-t-7", "blog", "web"),
        SyncCaddy(),
    ]


def test_deletes_are_planned_before_creates_and_stacks_sorted():
    keep = stack(name="alpha")
    doomed = stack(name="zulu", tenant="hosty-t-9")
    actions = plan([keep], converged(doomed))
    kinds = [type(a).__name__ for a in actions]
    assert kinds.index("RemoveUnits") < kinds.index("WriteUnits")
    assert actions[-1] == SyncCaddy()


def test_single_trailing_sync_caddy_for_multi_stack_plans():
    a, b = stack(name="alpha"), stack(name="beta", tenant="hosty-t-8")
    actions = plan([a, b], {})
    assert [x for x in actions if isinstance(x, SyncCaddy)] == [SyncCaddy()]
    assert actions[-1] == SyncCaddy()


def test_duplicate_desired_names_rejected():
    with pytest.raises(ValueError, match="Duplicate stack names"):
        plan([stack(), stack()], {})


def test_orphan_active_service_without_file_is_rewritten_and_restarted():
    # systemd reality: a unit can stay active after its file was deleted.
    spec = stack()
    observed = {
        "blog": ObservedStack(tenant="hosty-t-7", units={"web": ObservedUnit(None, active=True)})
    }
    host = ModelHost()
    host.tenants.add("hosty-t-7")
    host.stacks["blog"] = _StackState(tenant="hosty-t-7", active={"web"})
    assert host.observe() == observed
    host.apply(plan([spec], host.observe()))
    assert host.observe() == project([spec])


# --- the two theorems (property-based) ---------------------------------------------

STACK_NAMES = ("alpha", "beta", "gamma")
TENANTS = ("hosty-t-1", "hosty-t-2")
SERVICE_NAMES = ("web", "worker")
IMAGES = ("nginx:1.27", "nginx:1.28", "redis:7")
ENV_CHOICES = ((), (("APP_ENV", "prod"),), (("APP_ENV", "dev"), ("DEBUG", "1")))


@st.composite
def service_specs(draw, name: str) -> ServiceSpec:
    ported = draw(st.booleans())
    return ServiceSpec(
        name=name,
        image=draw(st.sampled_from(IMAGES)),
        env=draw(st.sampled_from(ENV_CHOICES)),
        internal_port=80 if ported else None,
        host_port=draw(st.integers(min_value=20001, max_value=20004)) if ported else None,
    )


@st.composite
def stack_specs(draw, name: str) -> StackSpec:
    services = tuple(
        draw(service_specs(svc))
        for svc in draw(
            st.lists(st.sampled_from(SERVICE_NAMES), min_size=1, max_size=2, unique=True)
        )
    )
    volumes = tuple(
        VolumeSpec(name=f"vol{i}", service=draw(st.sampled_from(services)).name, mount_path="/d")
        for i in range(draw(st.integers(min_value=0, max_value=2)))
    )
    return StackSpec(
        name=name,
        tenant=draw(st.sampled_from(TENANTS)),
        loopback_ip="127.1.0.1",
        services=services,
        volumes=volumes,
        suspended=draw(st.booleans()),
    )


@st.composite
def desired_lists(draw) -> list[StackSpec]:
    names = draw(st.lists(st.sampled_from(STACK_NAMES), max_size=3, unique=True))
    return [draw(stack_specs(name)) for name in names]


@st.composite
def hosts(draw) -> ModelHost:
    """A host seeded as converged with some PREVIOUS desired state, then
    mutated: drift, corruption, foreign artifacts, vanished tenants."""
    host = ModelHost()
    for spec in draw(desired_lists()):
        host.seed_converged(spec)
    for name in list(host.stacks):
        state = host.stacks[name]
        for svc in list(state.files):
            mutation = draw(st.sampled_from(["keep", "stop", "corrupt", "drop_file", "orphan"]))
            if mutation == "stop":
                state.active.discard(svc)
            elif mutation == "corrupt":
                state.files[svc] = draw(st.sampled_from([None, "0" * 32]))
            elif mutation == "drop_file":
                del state.files[svc]
                state.active.discard(svc)
            elif mutation == "orphan":  # file gone, container still running
                del state.files[svc]
                state.active.add(svc)
        if draw(st.booleans()):
            state.dirs.add("stray")
        if state.empty:
            del host.stacks[name]
    if draw(st.booleans()) and host.tenants:
        host.tenants.discard(sorted(host.tenants)[0])
    return host


@settings(max_examples=200, deadline=None)
@given(desired=desired_lists(), host=hosts())
def test_theorem_convergence_and_idempotency(desired: list[StackSpec], host: ModelHost):
    actions = plan(desired, host.observe())
    host.apply(actions)
    assert host.observe() == project(desired)  # convergence
    assert plan(desired, host.observe()) == []  # idempotency


@settings(max_examples=100, deadline=None)
@given(desired=desired_lists())
def test_theorem_plan_on_projection_is_empty(desired: list[StackSpec]):
    assert plan(desired, project(desired)) == []
