"""Unit coverage for the host observer (v2/M3, ADR-013).

The orchestration suite (test_orchestration.py) injects a fake
`build_observed()` and never exercises `app.orchestration.observer` itself —
the only thing that runs the real observer is the `vm`-marked
test_vm_reconcile.py, which cannot run off a privileged Linux host. This
file closes that gap: it fakes the three host seams the observer reads
(`tenants.exists`, `stackhost.scan_tenant`, `podman.ps`) and asserts the
`Observed` world it assembles, branch by branch.
"""

from __future__ import annotations

import pytest

from app.db.models import Tenant, User
from app.orchestration import observer as observer_mod
from app.system import podman as podman_mod
from app.system import stackhost as stackhost_mod
from app.system import tenants as tenants_sys
from app.system.podman import PsContainer
from app.system.stackhost import TenantScan, UnitFile


@pytest.fixture
async def db(app):
    async with app.state.sessionmaker() as session:
        yield session


async def _add_tenant(db, *, user_id: int, linux_user: str, uid: int | None, subuid_start: int):
    db.add(User(id=user_id, username=f"u{user_id}", password_hash="x", role="client"))
    await db.commit()  # satisfy the tenants.user_id FK before inserting the row
    db.add(
        Tenant(
            user_id=user_id,
            linux_user=linux_user,
            uid=uid,
            subuid_start=subuid_start,
            subuid_count=65536,
        )
    )
    await db.commit()


def _unit(file_name: str, stack: str | None, service: str | None, spec_hash: str | None):
    return UnitFile(file_name=file_name, stack=stack, service=service, spec_hash=spec_hash)


def _container(name: str, *, running: bool, stack: str | None):
    return PsContainer(
        name=name,
        image="img",
        state="running" if running else "exited",
        running=running,
        stack=stack,
    )


class _FakeHost:
    """Per-tenant fixtures for the three observer seams, keyed by linux_user."""

    def __init__(self) -> None:
        self.present: dict[str, bool] = {}
        self.scans: dict[str, TenantScan] = {}
        self.scan_raises: set[str] = set()
        self.containers: dict[int, list[PsContainer]] = {}
        self._uid_to_user: dict[int, str] = {}

    def add(
        self, linux_user: str, uid: int, *, present=True, scan=None, containers=None, raises=False
    ):
        self.present[linux_user] = present
        self._uid_to_user[uid] = linux_user
        if raises:
            self.scan_raises.add(linux_user)
        self.scans[linux_user] = scan or TenantScan()
        self.containers[uid] = containers or []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def exists(name):
            return self.present.get(name, False)

        def scan_tenant(uid, tenant):
            if tenant in self.scan_raises:
                raise OSError("scan blew up")
            return self.scans.get(tenant, TenantScan())

        async def ps(uid):
            return self.containers.get(uid, [])

        monkeypatch.setattr(tenants_sys, "exists", exists)
        monkeypatch.setattr(stackhost_mod, "scan_tenant", scan_tenant)
        monkeypatch.setattr(podman_mod, "ps", ps)


@pytest.fixture
def fake_host(monkeypatch) -> _FakeHost:
    host = _FakeHost()
    host.install(monkeypatch)
    return host


async def test_active_and_inactive_units_with_build_and_volumes(db, fake_host):
    await _add_tenant(db, user_id=1, linux_user="hosty-t-1", uid=6001, subuid_start=1_000_000)
    fake_host.add(
        "hosty-t-1",
        6001,
        scan=TenantScan(
            unit_files=(
                _unit("blog-web.container", "blog", "web", "h_web"),
                _unit("blog.network", "blog", None, None),  # presence only
                _unit("blog-api-build.service", "blog", "api", "h_api_build"),
                _unit("blog-api.container", "blog", "api", "h_api"),
            ),
            volume_dirs={"blog": frozenset({"content"})},
        ),
        containers=[_container("blog-web", running=True, stack="blog")],  # web up, api down
    )

    observed = await observer_mod.observe(db)

    assert set(observed) == {"blog"}
    blog = observed["blog"]
    assert blog.tenant == "hosty-t-1" and blog.tenant_present is True
    assert blog.units["web"].spec_hash == "h_web" and blog.units["web"].active is True
    assert blog.units["api"].spec_hash == "h_api" and blog.units["api"].active is False
    assert blog.build_units == {"api": "h_api_build"}
    assert blog.volume_dirs == frozenset({"content"})


async def test_network_only_stack_is_observed_with_no_units(db, fake_host):
    await _add_tenant(db, user_id=1, linux_user="hosty-t-1", uid=6001, subuid_start=1_000_000)
    fake_host.add(
        "hosty-t-1",
        6001,
        scan=TenantScan(unit_files=(_unit("leftover.network", "leftover", None, None),)),
    )

    observed = await observer_mod.observe(db)

    # A bare .network file still reports the stack so the planner can reap it.
    assert "leftover" in observed
    assert observed["leftover"].units == {}
    assert observed["leftover"].build_units == {}


async def test_orphan_running_container_without_unit_file(db, fake_host):
    await _add_tenant(db, user_id=1, linux_user="hosty-t-1", uid=6001, subuid_start=1_000_000)
    fake_host.add(
        "hosty-t-1",
        6001,
        # No unit files at all; a labelled container survives unit removal.
        containers=[_container("ghost-cache", running=True, stack="ghost")],
    )

    observed = await observer_mod.observe(db)

    assert "ghost" in observed
    unit = observed["ghost"].units["cache"]
    assert unit.active is True
    assert unit.spec_hash is None  # no unit file → always stale → planner rewrites


async def test_unit_file_active_state_tracks_running_container(db, fake_host):
    await _add_tenant(db, user_id=1, linux_user="hosty-t-1", uid=6001, subuid_start=1_000_000)
    fake_host.add(
        "hosty-t-1",
        6001,
        scan=TenantScan(unit_files=(_unit("shop-db.container", "shop", "db", "h_db"),)),
        containers=[_container("shop-db", running=False, stack="shop")],  # exited != active
    )

    observed = await observer_mod.observe(db)
    assert observed["shop"].units["db"].active is False


async def test_volume_only_stack_is_observed(db, fake_host):
    await _add_tenant(db, user_id=1, linux_user="hosty-t-1", uid=6001, subuid_start=1_000_000)
    fake_host.add(
        "hosty-t-1",
        6001,
        scan=TenantScan(volume_dirs={"data": frozenset({"pgdata"})}),
    )

    observed = await observer_mod.observe(db)
    assert observed["data"].units == {}
    assert observed["data"].volume_dirs == frozenset({"pgdata"})


async def test_absent_host_user_reports_tenant_not_present_and_skips_podman(db, fake_host):
    await _add_tenant(db, user_id=1, linux_user="hosty-t-1", uid=6001, subuid_start=1_000_000)
    # Ledger row + leftover unit file, but the host user is gone (present=False):
    # podman.ps must NOT be consulted, so the unit reads inactive.
    fake_host.add(
        "hosty-t-1",
        6001,
        present=False,
        scan=TenantScan(unit_files=(_unit("old-web.container", "old", "web", "h_old"),)),
        # A stale container would be ignored because present=False short-circuits ps.
        containers=[_container("old-web", running=True, stack="old")],
    )

    observed = await observer_mod.observe(db)
    assert observed["old"].tenant_present is False
    assert observed["old"].units["web"].active is False


async def test_unprovisioned_tenant_uid_none_is_skipped(db, fake_host):
    await _add_tenant(db, user_id=1, linux_user="hosty-t-1", uid=None, subuid_start=1_000_000)

    observed = await observer_mod.observe(db)
    assert observed == {}


async def test_one_tenant_failure_does_not_break_observation_of_others(db, fake_host):
    await _add_tenant(db, user_id=1, linux_user="hosty-t-1", uid=6001, subuid_start=1_000_000)
    await _add_tenant(db, user_id=2, linux_user="hosty-t-2", uid=6002, subuid_start=2_000_000)
    fake_host.add("hosty-t-1", 6001, raises=True)  # observation of t-1 explodes
    fake_host.add(
        "hosty-t-2",
        6002,
        scan=TenantScan(unit_files=(_unit("blog-web.container", "blog", "web", "h"),)),
        containers=[_container("blog-web", running=True, stack="blog")],
    )

    observed = await observer_mod.observe(db)

    # t-1 degraded to "absent" (logged, not raised); t-2 still observed.
    assert "blog" in observed
    assert observed["blog"].tenant == "hosty-t-2"
    assert observed["blog"].units["web"].active is True
