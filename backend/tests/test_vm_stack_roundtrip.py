"""v2/M2 VM gate (ADR-013): a StackSpec round-trips on the real host —
render → place → reload → start → serve → observe → tear down. Runs only
on the privileged dev VM: `dev-vm.ps1 test` (pytest -m vm, as root)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path

import httpx
import pytest

from app.domain.specs import ServiceSpec, StackSpec, VolumeSpec, spec_hash
from app.system import podman, quadlet, systemd_user, tenants

pytestmark = pytest.mark.vm

TENANT = "hosty-t-9998"  # spike-adjacent id, well out of real range
SUB_START = 899_000_000  # disjoint from the ledger and from spike-quadlet.sh
PORT = 24998


def _spec() -> StackSpec:
    return StackSpec(
        name="m2trip",
        tenant=TENANT,
        services=(
            ServiceSpec(
                name="web",
                image="docker.io/library/nginx:1.27-alpine",
                internal_port=80,
                host_port=PORT,
                memory_mb=256,
            ),
        ),
        volumes=(VolumeSpec(name="scratch", service="web", mount_path="/data"),),
    )


async def _wait_for(predicate, *, attempts: int = 60, delay: float = 1.0, what: str = ""):
    for _ in range(attempts):
        if await predicate():
            return
        await asyncio.sleep(delay)
    pytest.fail(f"timed out waiting for {what}")


async def test_stack_spec_round_trips_on_the_host():
    spec = _spec()
    unit = quadlet.service_unit_name(spec.name, "web")
    info = await tenants.provision(TENANT, subuid_start=SUB_START, subuid_count=65536)
    unit_dir = quadlet.unit_dir(info.uid)
    try:
        # Tenant user manager must be up (linger was just enabled).
        bus = Path(f"/run/user/{info.uid}/bus")

        async def _bus_up() -> bool:
            return bus.is_socket()

        await _wait_for(_bus_up, what="tenant user manager")

        # Volume dir, tenant-owned (what EnsureVolumeDir will do in M3).
        volume_dir = quadlet.volume_host_dir(TENANT, spec.name, "scratch")
        os.makedirs(volume_dir, exist_ok=True)
        for path in (
            quadlet.stacks_root(TENANT),
            quadlet.stack_dir(TENANT, spec.name),
            f"{quadlet.stack_dir(TENANT, spec.name)}/volumes",
            volume_dir,
        ):
            shutil.chown(path, user=TENANT, group=TENANT)

        # Place the full unit set (what WriteUnits will do in M3).
        os.makedirs(unit_dir, exist_ok=True)
        for file_name, content in quadlet.unit_files(spec).items():
            Path(unit_dir, file_name).write_text(content, encoding="utf-8")

        await systemd_user.daemon_reload(TENANT)
        # The per-tenant API socket podman.py observes through.
        await systemd_user.control(TENANT, "start", "podman.socket")
        await systemd_user.control(TENANT, "start", unit)

        async def _serving() -> bool:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"http://127.0.0.1:{PORT}/")
                return response.status_code == 200
            except httpx.HTTPError:
                return False

        await _wait_for(_serving, what="nginx on the loopback publish")

        # Observe back through every adapter surface.
        status = await systemd_user.status(TENANT, unit)
        assert status.available and status.active_state == "active"

        containers = await podman.ps(info.uid)
        ours = [c for c in containers if c.name == f"{spec.name}-web"]
        assert ours and ours[0].running and ours[0].stack == spec.name

        on_disk = Path(unit_dir, quadlet.container_file_name(spec.name, "web")).read_text(
            encoding="utf-8"
        )
        assert quadlet.read_spec_hash(on_disk) == spec_hash(spec, spec.services[0])

        journal = await systemd_user.journal(TENANT, unit, uid=info.uid, tail=50)
        assert journal.strip(), "journald must show container output"
    finally:
        with contextlib.suppress(systemd_user.SystemdUserError):
            await systemd_user.control(TENANT, "stop", unit)
        shutil.rmtree(unit_dir, ignore_errors=True)
        with contextlib.suppress(systemd_user.SystemdUserError):
            await systemd_user.daemon_reload(TENANT)
        await tenants.remove(TENANT)
