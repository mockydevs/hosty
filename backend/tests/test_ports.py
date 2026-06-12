"""Shared loopback port allocator (v2/M2): lowest-free from the DB ledger,
union across workload tables, exhaustion error; apps delegation keeps its
error contract."""

from __future__ import annotations

import pytest

from app.db.models import App, User
from app.services import apps as apps_service
from app.services import ports
from app.services.apps import AppValidationError


@pytest.fixture
async def db(app):
    async with app.state.sessionmaker() as session:
        yield session


async def seed_app(db, port: int, *, name_suffix: str) -> App:
    owner = User(username=f"owner-{name_suffix}", password_hash="x", role="client")
    db.add(owner)
    await db.commit()
    await db.refresh(owner)
    record = App(
        owner_id=owner.id,
        name=f"app-{name_suffix}",
        domain=f"{name_suffix}.example.com",
        image="nginx:1",
        internal_port=80,
        host_port=port,
        status="running",
        volumes_json="[]",
    )
    db.add(record)
    await db.commit()
    return record


async def test_allocates_lowest_free_port(db, settings):
    assert await ports.allocate_host_port(db, settings) == settings.app_port_min
    await seed_app(db, settings.app_port_min, name_suffix="a")
    assert await ports.allocate_host_port(db, settings) == settings.app_port_min + 1


async def test_fills_gaps_first(db, settings):
    await seed_app(db, settings.app_port_min, name_suffix="a")
    await seed_app(db, settings.app_port_min + 2, name_suffix="b")
    assert await ports.allocate_host_port(db, settings) == settings.app_port_min + 1


async def test_exhaustion_raises(db, settings):
    settings.app_port_max = settings.app_port_min + 1
    await seed_app(db, settings.app_port_min, name_suffix="a")
    await seed_app(db, settings.app_port_min + 1, name_suffix="b")
    with pytest.raises(ports.NoFreePortError):
        await ports.allocate_host_port(db, settings)


async def test_used_host_ports_union(db, settings):
    await seed_app(db, settings.app_port_min, name_suffix="a")
    assert await ports.used_host_ports(db) == {settings.app_port_min}


async def test_apps_allocator_delegates_and_keeps_error_contract(db, settings):
    assert await apps_service.allocate_host_port(db, settings) == settings.app_port_min
    settings.app_port_max = settings.app_port_min - 1  # empty range
    with pytest.raises(AppValidationError):
        await apps_service.allocate_host_port(db, settings)
