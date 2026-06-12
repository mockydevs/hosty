"""Tenant ledger service (v2/M0): allocation is monotonic and disjoint,
ensure is idempotent and repairs host drift, removal frees the ledger only
after the host user is gone. Host layer fully faked."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Tenant, User
from app.services import tenancy
from app.system import tenants as tenants_sys


class FakeHost:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}  # linux_user -> {uid, subuids, linger}
        self.next_uid = 5000
        self.fail_provision = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        async def provision(name, *, subuid_start, subuid_count):
            if fake.fail_provision:
                raise tenants_sys.TenantOperationError("injected host failure")
            entry = fake.users.setdefault(name, {"uid": fake.next_uid, "subuids": None})
            if entry["uid"] == fake.next_uid:
                fake.next_uid += 1
            entry["subuids"] = (subuid_start, subuid_count)
            entry["linger"] = True
            return tenants_sys.TenantInfo(
                linux_user=name,
                uid=entry["uid"],
                subuid_start=subuid_start,
                subuid_count=subuid_count,
            )

        async def remove(name):
            return fake.users.pop(name, None) is not None

        monkeypatch.setattr(tenants_sys, "provision", provision)
        monkeypatch.setattr(tenants_sys, "remove", remove)


@pytest.fixture
def fake_host(monkeypatch) -> FakeHost:
    fake = FakeHost()
    fake.install(monkeypatch)
    return fake


async def make_user(db, username: str) -> User:
    user = User(username=username, password_hash="x", role="client")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
async def db(app):
    async with app.state.sessionmaker() as session:
        yield session


async def test_ensure_tenant_allocates_disjoint_monotonic_ranges(db, fake_host):
    t1 = await tenancy.ensure_tenant(db, await make_user(db, "alice"))
    t2 = await tenancy.ensure_tenant(db, await make_user(db, "bob"))
    assert t1.subuid_start == tenants_sys.SUBID_BASE
    assert t2.subuid_start == t1.subuid_start + tenants_sys.SUBID_COUNT
    assert t1.linux_user == f"hosty-t-{t1.user_id}"
    assert t1.uid == fake_host.users[t1.linux_user]["uid"]


async def test_ensure_tenant_is_idempotent_and_repairs_host(db, fake_host):
    user = await make_user(db, "alice")
    t1 = await tenancy.ensure_tenant(db, user)
    # Host user vanishes (manual meddling / restored VM) — same range reused.
    fake_host.users.clear()
    t2 = await tenancy.ensure_tenant(db, user)
    assert t2.subuid_start == t1.subuid_start
    assert t1.linux_user in fake_host.users
    rows = (await db.execute(select(Tenant))).scalars().all()
    assert len(rows) == 1


async def test_ledger_row_survives_host_failure_and_range_is_not_reissued(db, fake_host):
    user = await make_user(db, "alice")
    fake_host.fail_provision = True
    with pytest.raises(tenants_sys.TenantOperationError):
        await tenancy.ensure_tenant(db, user)
    # Range reserved even though the host op failed.
    row = await db.get(Tenant, user.id)
    assert row is not None and row.uid is None
    # A second tenant does NOT receive the reserved range.
    fake_host.fail_provision = False
    t2 = await tenancy.ensure_tenant(db, await make_user(db, "bob"))
    assert t2.subuid_start == row.subuid_start + tenants_sys.SUBID_COUNT
    # Retry repairs the first tenant in place.
    t1 = await tenancy.ensure_tenant(db, user)
    assert t1.subuid_start == row.subuid_start and t1.uid is not None


async def test_remove_tenant(db, fake_host):
    user = await make_user(db, "alice")
    t = await tenancy.ensure_tenant(db, user)
    assert await tenancy.remove_tenant(db, user.id) is True
    assert t.linux_user not in fake_host.users
    assert await db.get(Tenant, user.id) is None
    assert await tenancy.remove_tenant(db, user.id) is False
