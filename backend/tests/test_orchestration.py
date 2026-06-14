"""M3 gate (v2, ADR-013): reconciler + executor against a FakeHost at the
adapter seams — the real planner plans, the real executor dispatches, the
fakes keep host state in memory, and the DB (tenant ledger, operations,
notifications) is real.

The headline test: kill a container between cycles → the next cycle
replans, restarts it, and (after N consecutive diverged cycles) notifies."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.secrets import encrypt_secret
from app.db.models import Notification, Operation, SshKey, Tenant, User
from app.domain.specs import (
    Observed,
    ObservedStack,
    ObservedUnit,
    ServiceSpec,
    StackSpec,
    VolumeSpec,
)
from app.orchestration.reconciler import Reconciler
from app.system import quadlet
from app.system import stackhost as stackhost_mod
from app.system import systemd_user as systemd_user_mod
from app.system import tenants as tenants_sys
from app.system.stackhost import TenantScan, UnitFile


class FakeHost:
    """In-memory host behind the executor's adapter seams."""

    def __init__(self) -> None:
        self.users: dict[str, int] = {}  # linux_user -> uid
        self.next_uid = 6000
        self.unit_files: dict[int, dict[str, str]] = {}  # uid -> filename -> content
        self.env_files: dict[str, str] = {}
        self.ssh_keys: dict[str, list[tuple[str, str]]] = {}
        self.volume_dirs: set[tuple[str, str, str]] = set()  # (tenant, stack, volume)
        self.active: set[tuple[str, str]] = set()  # (tenant, unit_name)
        self.fail_control: set[str] = set()  # actions ("start"/"stop"/...) that raise

    # --- seam: app.system.tenants -------------------------------------------------
    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host = self

        async def provision(name, *, subuid_start, subuid_count):
            uid = host.users.setdefault(name, host.next_uid)
            if uid == host.next_uid:
                host.next_uid += 1
            return tenants_sys.TenantInfo(
                linux_user=name, uid=uid, subuid_start=subuid_start, subuid_count=subuid_count
            )

        async def remove(name):
            return host.users.pop(name, None) is not None

        async def exists(name):
            return name in host.users

        monkeypatch.setattr(tenants_sys, "provision", provision)
        monkeypatch.setattr(tenants_sys, "remove", remove)
        monkeypatch.setattr(tenants_sys, "exists", exists)

        # --- seam: app.system.stackhost (sync fns, called via to_thread) ----------
        def sync_units(uid, stack, tenant, desired):
            files = host.unit_files.setdefault(uid, {})
            for name in [n for n, c in files.items() if quadlet.read_stack_marker(c) == stack]:
                if name not in desired:
                    del files[name]
            files.update(desired)
            return True

        def remove_units(uid, stack, tenant):
            return sync_units(uid, stack, tenant, {})

        def sync_env_files(tenant, stack, files):
            prefix = f"/stacks/{stack}/env/"
            for path in [p for p in host.env_files if prefix in p.replace("\\", "/")]:
                if path not in files:
                    del host.env_files[path]
            host.env_files.update(files)
            return True

        def remove_env_files(tenant, stack):
            return sync_env_files(tenant, stack, {})

        def ensure_volume_dir(tenant, stack, volume):
            host.volume_dirs.add((tenant, stack, volume))

        def sync_ssh_keys(tenant, keys):
            host.ssh_keys[tenant] = list(keys)

        async def remove_volume_dir(tenant, stack, volume):
            host.volume_dirs.discard((tenant, stack, volume))

        def scan_tenant(uid, tenant):
            unit_files = []
            for file_name, content in host.unit_files.get(uid, {}).items():
                unit_files.append(
                    UnitFile(
                        file_name=file_name,
                        stack=quadlet.read_stack_marker(content),
                        service=quadlet.read_service_marker(content),
                        spec_hash=quadlet.read_spec_hash(content),
                    )
                )
            dirs: dict[str, set[str]] = {}
            for vol_tenant, stack, volume in host.volume_dirs:
                if vol_tenant == tenant:
                    dirs.setdefault(stack, set()).add(volume)
            return TenantScan(
                unit_files=tuple(unit_files),
                volume_dirs={s: frozenset(v) for s, v in dirs.items()},
            )

        for name, fn in [
            ("sync_units", sync_units),
            ("remove_units", remove_units),
            ("sync_env_files", sync_env_files),
            ("remove_env_files", remove_env_files),
            ("ensure_volume_dir", ensure_volume_dir),
            ("sync_ssh_keys", sync_ssh_keys),
            ("remove_volume_dir", remove_volume_dir),
            ("scan_tenant", scan_tenant),
        ]:
            monkeypatch.setattr(stackhost_mod, name, fn)

        # --- seam: app.system.systemd_user ----------------------------------------
        async def daemon_reload(user):
            return None

        async def control(user, action, unit):
            if action in host.fail_control:
                raise systemd_user_mod.SystemdUserError(f"injected {action} failure")
            uid = host.users.get(user)
            if action in ("start", "restart"):
                files = host.unit_files.get(uid, {})
                base = unit.removesuffix(".service")
                if not any(
                    f"{quadlet.read_stack_marker(c)}-{quadlet.read_service_marker(c)}" == base
                    for c in files.values()
                ):
                    raise systemd_user_mod.SystemdUserError(f"no unit file for {unit}")
                host.active.add((user, unit))
            elif action == "stop":
                host.active.discard((user, unit))

        monkeypatch.setattr(systemd_user_mod, "daemon_reload", daemon_reload)
        monkeypatch.setattr(systemd_user_mod, "control", control)
        # executor imports the modules, not the functions — patching the
        # module attributes above covers it.

    # --- the injected observe ----------------------------------------------------
    def build_observed(self) -> Observed:
        stacks: dict[str, dict] = {}
        for linux_user, uid in self.users.items():
            for file_name, content in self.unit_files.get(uid, {}).items():
                stack = quadlet.read_stack_marker(content)
                if stack is None:
                    continue
                entry = stacks.setdefault(
                    stack, {"tenant": linux_user, "units": {}, "builds": {}, "dirs": set()}
                )
                service = quadlet.read_service_marker(content)
                if service is not None and (
                    file_name.endswith(".build")
                    or file_name.endswith("-build.service")
                    or file_name.endswith("-nixpacks.service")
                ):
                    entry["builds"][service] = quadlet.read_spec_hash(content)
                    continue
                if service is not None:
                    unit_name = f"{stack}-{service}.service"
                    entry["units"][service] = ObservedUnit(
                        spec_hash=quadlet.read_spec_hash(content),
                        active=(linux_user, unit_name) in self.active,
                    )
        for tenant, stack, volume in self.volume_dirs:
            entry = stacks.setdefault(
                stack, {"tenant": tenant, "units": {}, "builds": {}, "dirs": set()}
            )
            entry["dirs"].add(volume)
        return {
            name: ObservedStack(
                tenant=entry["tenant"],
                tenant_present=entry["tenant"] in self.users,
                units=entry["units"],
                build_units=entry["builds"],
                volume_dirs=frozenset(entry["dirs"]),
            )
            for name, entry in stacks.items()
        }


def make_spec(owner_id: int, *, suspended: bool = False) -> StackSpec:
    return StackSpec(
        name="blog",
        tenant=f"hosty-t-{owner_id}",
        loopback_ip="127.1.0.1",
        services=(ServiceSpec(name="web", image="nginx:1.27", internal_port=80),),
        volumes=(VolumeSpec(name="content", service="web", mount_path="/var/www"),),
        suspended=suspended,
    )


@pytest.fixture
async def db(app):
    async with app.state.sessionmaker() as session:
        yield session


@pytest.fixture
def host(monkeypatch) -> FakeHost:
    fake = FakeHost()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def make_reconciler(app, settings, host):
    settings.reconcile_concurrency = 1  # sqlite: one writer

    def build(desired_specs: list[StackSpec], **kwargs) -> Reconciler:
        async def load_desired(db):
            return list(desired_specs)

        async def observe(db):
            return host.build_observed()

        return Reconciler(
            app.state.sessionmaker, settings, load_desired=load_desired, observe=observe, **kwargs
        )

    return build


async def make_owner(db) -> User:
    user = User(username="alice", password_hash="x", role="client")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_fresh_create_converges_end_to_end(db, host, make_reconciler):
    owner = await make_owner(db)
    spec = make_spec(owner.id)
    desired = [spec]
    reconciler = make_reconciler(desired)

    (outcome,) = await reconciler.converge_all()
    assert outcome.error is None and outcome.planned > 0

    # Host state: tenant, units, env-less, volume dir, service running.
    assert spec.tenant in host.users
    uid = host.users[spec.tenant]
    assert sorted(host.unit_files[uid]) == ["blog-web.container", "blog.network"]
    assert (spec.tenant, "blog", "content") in host.volume_dirs
    assert (spec.tenant, "blog-web.service") in host.active
    # Ledger row exists with the uid.
    row = (await db.execute(select(Tenant).where(Tenant.linux_user == spec.tenant))).scalar_one()
    assert row.uid == uid

    # Second cycle: converged, nothing planned.
    (outcome,) = await reconciler.converge_all()
    assert outcome.planned == 0


async def test_tenant_provision_installs_preexisting_private_git_keys(
    db, host, make_reconciler, settings
):
    owner = await make_owner(db)
    db.add(
        SshKey(
            owner_id=owner.id,
            name="github-main",
            public_key="ssh-ed25519 test",
            private_key_encrypted=encrypt_secret("private-key", settings.secret_key),
        )
    )
    await db.commit()

    reconciler = make_reconciler([make_spec(owner.id)])
    await reconciler.converge_all()

    assert host.ssh_keys[f"hosty-t-{owner.id}"] == [("github-main", "private-key")]


async def test_killed_container_is_restarted_next_cycle_and_drift_notifies(
    db, host, make_reconciler, settings
):
    owner = await make_owner(db)
    reconciler = make_reconciler([make_spec(owner.id)])
    await reconciler.converge_all()  # create (counts as one diverged cycle)
    (outcome,) = await reconciler.converge_all()  # clean cycle resets the counter
    assert outcome.planned == 0
    tenant = f"hosty-t-{owner.id}"

    for cycle in range(1, settings.reconcile_drift_cycles + 1):
        host.active.discard((tenant, "blog-web.service"))  # kill it
        (outcome,) = await reconciler.converge_all()
        assert outcome.planned > 0 and outcome.error is None
        assert (tenant, "blog-web.service") in host.active  # healed

        db.expire_all()
        notes = (
            (await db.execute(select(Notification).where(Notification.kind == "stack_drift")))
            .scalars()
            .all()
        )
        if cycle < settings.reconcile_drift_cycles:
            assert notes == []
        else:
            assert len(notes) == 1 and notes[0].dedupe_key == "stack:blog:drift"

    # A clean cycle resolves the drift notification.
    (outcome,) = await reconciler.converge_all()
    assert outcome.planned == 0
    db.expire_all()
    note = (
        await db.execute(select(Notification).where(Notification.kind == "stack_drift"))
    ).scalar_one()
    assert note.resolved_at is not None


async def test_action_failure_degrades_and_next_cycle_repairs(db, host, make_reconciler):
    owner = await make_owner(db)
    statuses: list[tuple[str, str, str | None]] = []

    async def on_status(db_, stack, status, error):
        statuses.append((stack, status, error))

    reconciler = make_reconciler([make_spec(owner.id)], on_stack_status=on_status)
    host.fail_control.add("start")
    (outcome,) = await reconciler.converge_all()
    assert outcome.error is not None and "injected start failure" in outcome.error
    assert statuses[-1][1] == "degraded"

    host.fail_control.clear()
    (outcome,) = await reconciler.converge_all()
    assert outcome.error is None
    assert statuses[-1][1] == "ready"
    # No compensation happened in between: units written once, still there.
    uid = host.users[f"hosty-t-{owner.id}"]
    assert "blog-web.container" in host.unit_files[uid]


async def test_delete_tears_down_and_releases_tenant(db, host, make_reconciler):
    owner = await make_owner(db)
    spec = make_spec(owner.id)
    desired = [spec]
    reconciler = make_reconciler(desired)
    await reconciler.converge_all()
    assert spec.tenant in host.users
    host.env_files["/home/hosty-t-1/stacks/blog/env/web.env"] = "SECRET=old\n"

    desired.clear()  # the API deleted the stack
    (outcome,) = await reconciler.converge_all()
    assert outcome.error is None
    assert host.build_observed() == {}
    assert host.env_files == {}
    assert spec.tenant not in host.users  # host user removed
    assert (
        await db.execute(select(Tenant).where(Tenant.linux_user == spec.tenant))
    ).scalar_one_or_none() is None  # ledger released

    assert await reconciler.converge_all() == []  # world empty, plan empty


async def test_suspension_stops_services_and_reports_suspended(db, host, make_reconciler):
    owner = await make_owner(db)
    statuses: list[tuple[str, str, str | None]] = []

    async def on_status(db_, stack, status, error):
        statuses.append((stack, status, error))

    desired = [make_spec(owner.id)]
    reconciler = make_reconciler(desired, on_stack_status=on_status)
    await reconciler.converge_all()
    tenant = f"hosty-t-{owner.id}"
    assert (tenant, "blog-web.service") in host.active

    desired[0] = make_spec(owner.id, suspended=True)
    (outcome,) = await reconciler.converge_all()
    assert outcome.error is None
    assert (tenant, "blog-web.service") not in host.active
    assert (tenant, "blog", "content") in host.volume_dirs  # volumes kept
    assert statuses[-1] == ("blog", "suspended", None)

    desired[0] = make_spec(owner.id)  # resume
    await reconciler.converge_all()
    assert (tenant, "blog-web.service") in host.active
    assert statuses[-1] == ("blog", "ready", None)


async def test_operation_records_planner_actions_as_steps(db, host, make_reconciler):
    owner = await make_owner(db)
    op = Operation(kind="converge_stack", domain="blog", status="pending")
    db.add(op)
    await db.commit()
    await db.refresh(op)

    reconciler = make_reconciler([make_spec(owner.id)])
    outcome = await reconciler.converge_stack("blog", operation_id=op.id)
    assert outcome.error is None

    await db.refresh(op)
    assert op.status == "succeeded"
    steps = json.loads(op.steps_json)
    assert [s["name"] for s in steps] == [
        "tenant",
        "volume:content",
        "units",
        "daemon-reload",
        "start:web",
        "ingress",
    ]
    assert {s["status"] for s in steps} == {"done"}


async def test_failed_operation_marks_step_failed(db, host, make_reconciler):
    owner = await make_owner(db)
    op = Operation(kind="converge_stack", domain="blog", status="pending")
    db.add(op)
    await db.commit()
    await db.refresh(op)

    host.fail_control.add("start")
    reconciler = make_reconciler([make_spec(owner.id)])
    outcome = await reconciler.converge_stack("blog", operation_id=op.id)
    assert outcome.error is not None

    await db.refresh(op)
    assert op.status == "failed"
    steps = {s["name"]: s["status"] for s in json.loads(op.steps_json)}
    assert steps["start:web"] == "failed"
    assert steps["units"] == "done"
    assert steps["ingress"] == "pending"  # never reached — no undo, no skip-ahead


async def test_sync_caddy_called_when_plan_nonempty(db, host, make_reconciler):
    owner = await make_owner(db)
    calls = []

    async def sync_caddy(db_):
        calls.append(1)

    reconciler = make_reconciler([make_spec(owner.id)], sync_caddy=sync_caddy)
    await reconciler.converge_all()
    assert calls == [1]
    await reconciler.converge_all()  # converged: no actions, no caddy churn
    assert calls == [1]
