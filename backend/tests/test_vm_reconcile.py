"""v2/M3 VM gate (ADR-013): kill a container by hand on the real host; the
reconciler converges within one cycle using the REAL observer and executor.
Runs only on the privileged dev VM: `dev-vm.ps1 test`."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.models import Tenant, User
from app.domain.specs import ServiceSpec, StackSpec
from app.orchestration.reconciler import Reconciler
from app.system import podman

pytestmark = pytest.mark.vm

PORT = 24997


async def test_reconciler_heals_a_killed_container(app, settings):
    settings.reconcile_concurrency = 1
    async with app.state.sessionmaker() as db:
        owner = User(username="vm-reconcile-owner", password_hash="x", role="client")
        db.add(owner)
        await db.commit()
        await db.refresh(owner)
        owner_id = owner.id

    spec = StackSpec(
        name="m3heal",
        tenant=f"hosty-t-{owner_id}",
        services=(
            ServiceSpec(
                name="web",
                image="docker.io/library/nginx:1.27-alpine",
                internal_port=80,
                host_port=PORT,
            ),
        ),
    )
    desired: list[StackSpec] = [spec]

    async def load_desired(db):
        return list(desired)

    reconciler = Reconciler(app.state.sessionmaker, settings, load_desired=load_desired)
    try:
        outcome = next(o for o in await reconciler.converge_all() if o.stack == "m3heal")
        assert outcome.error is None, outcome.error

        async with app.state.sessionmaker() as db:
            tenant_row = (
                await db.execute(select(Tenant).where(Tenant.linux_user == spec.tenant))
            ).scalar_one()
        uid = tenant_row.uid

        async def running() -> bool:
            return any(c.name == "m3heal-web" and c.running for c in await podman.ps(uid))

        for _ in range(60):
            if await running():
                break
            await asyncio.sleep(1)
        assert await running(), "stack never came up"

        # Diverge by hand. NB: a bare `podman stop` is healed by systemd
        # itself (Restart=always) — the reconciler's job is the case systemd
        # will NOT fix: a unit somebody stopped (or that hit its restart
        # limit). Stop the unit out-of-band.
        from app.system import runner

        result = await runner.run(
            [
                "systemctl",
                "--machine",
                f"{spec.tenant}@.host",
                "--user",
                "stop",
                "m3heal-web.service",
            ],
            timeout=60,
        )
        assert result.ok, result.stderr
        assert not await running()

        # One reconcile cycle must heal it.
        outcome = next(o for o in await reconciler.converge_all() if o.stack == "m3heal")
        assert outcome.error is None and outcome.planned > 0
        for _ in range(60):
            if await running():
                break
            await asyncio.sleep(1)
        assert await running(), "reconciler did not restart the killed container"
    finally:
        desired.clear()
        await reconciler.converge_all()  # tears down stack, units, tenant
