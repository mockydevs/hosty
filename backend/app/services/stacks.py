"""Stacks business layer (v2/M4, ADR-013): the bridge between DB rows
(desired state, encrypted at rest) and the pure domain specs the planner
consumes. Also the reconciler's wiring: desired-state loader, Caddy route
assembly, and the status projection hook.

The blueprint runs ONLY here, at create/upgrade time, persisting rendered
rows. The reconciler reads rows, never the blueprint.
"""

from __future__ import annotations

import json
import secrets as pysecrets

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import AppError
from app.core.secrets import decrypt_secret, encrypt_secret
from app.db.models import Stack, StackEndpoint, StackService, StackVolume, User
from app.domain.specs import EndpointSpec, ServiceSpec, StackSpec, VolumeSpec
from app.domain.validate import SpecValidationError
from app.orchestration.blueprints import get_blueprint
from app.orchestration.blueprints.base import Allocation, Blueprint
from app.services import caddy, ports

log = structlog.get_logger("hosty.stacks")

# Stacks being deleted leave desired state; every other status converges.
ACTIVE_STATUSES = ("converging", "ready", "degraded", "suspended", "error")


class StackValidationError(AppError):
    status_code = 422
    code = "invalid_stack"


def tenant_for(owner_id: int) -> str:
    return f"hosty-t-{int(owner_id)}"


def encrypt_env(env: dict[str, str], settings: Settings) -> str | None:
    return encrypt_secret(json.dumps(env), settings.secret_key) if env else None


def decrypt_env(service: StackService, settings: Settings) -> dict[str, str]:
    if not service.env_encrypted:
        return {}
    return json.loads(decrypt_secret(service.env_encrypted, settings.secret_key))


def encrypt_inputs(inputs: dict, settings: Settings) -> str:
    return encrypt_secret(json.dumps(inputs), settings.secret_key)


def decrypt_inputs(stack: Stack, settings: Settings) -> dict:
    if not stack.inputs_encrypted:
        return {}
    return json.loads(decrypt_secret(stack.inputs_encrypted, settings.secret_key))


async def stack_children(
    db: AsyncSession, stack_id: int
) -> tuple[list[StackService], list[StackVolume], list[StackEndpoint]]:
    services = (
        (await db.execute(select(StackService).where(StackService.stack_id == stack_id)))
        .scalars()
        .all()
    )
    volumes = (
        (await db.execute(select(StackVolume).where(StackVolume.stack_id == stack_id)))
        .scalars()
        .all()
    )
    endpoints = (
        (await db.execute(select(StackEndpoint).where(StackEndpoint.stack_id == stack_id)))
        .scalars()
        .all()
    )
    return list(services), list(volumes), list(endpoints)


def spec_for(
    stack: Stack,
    services: list[StackService],
    volumes: list[StackVolume],
    endpoints: list[StackEndpoint],
    settings: Settings,
    *,
    owner_suspended: bool,
) -> StackSpec:
    """Rows -> validated StackSpec. Raises SpecValidationError on corrupt
    rows (the caller logs and excludes — one bad stack must not stop the
    world)."""
    return StackSpec(
        name=stack.name,
        tenant=tenant_for(stack.owner_id),
        services=tuple(
            ServiceSpec(
                name=svc.name,
                image=svc.image_digest or svc.image,
                env=tuple(sorted(decrypt_env(svc, settings).items())),
                internal_port=svc.internal_port,
                host_port=svc.host_port,
                memory_mb=svc.memory_mb,
                cpu_percent=svc.cpu_percent,
                is_web=svc.is_web,
            )
            for svc in sorted(services, key=lambda s: s.name)
        ),
        volumes=tuple(
            VolumeSpec(name=vol.name, service=vol.service_name, mount_path=vol.mount_path)
            for vol in sorted(volumes, key=lambda v: v.name)
        ),
        endpoints=tuple(
            EndpointSpec(
                domain=ep.domain,
                service=ep.service_name,
                behind_cloudflare=ep.behind_cloudflare,
            )
            for ep in sorted(endpoints, key=lambda e: e.domain)
        ),
        suspended=owner_suspended,
    )


async def load_desired(db: AsyncSession, settings: Settings) -> list[StackSpec]:
    """The reconciler's desired world. Owner-less stacks (account deleted)
    leave desired state and are torn down; suspended owners scale to zero."""
    rows = (
        await db.execute(
            select(Stack, User.suspended)
            .join(User, User.id == Stack.owner_id)
            .where(Stack.status.in_(ACTIVE_STATUSES))
        )
    ).all()
    specs: list[StackSpec] = []
    for stack, owner_suspended in rows:
        children = await stack_children(db, stack.id)
        try:
            specs.append(
                spec_for(stack, *children, settings, owner_suspended=bool(owner_suspended))
            )
        except SpecValidationError as exc:
            log.error("stack_rows_invalid", stack=stack.name, error=str(exc))
    return specs


async def stack_routes(db: AsyncSession, settings: Settings) -> list[caddy.StackRoute]:
    """Caddy routes for every active stack endpoint (suspension -> 503)."""
    routes: list[caddy.StackRoute] = []
    rows = (
        await db.execute(
            select(Stack, User.suspended)
            .join(User, User.id == Stack.owner_id)
            .where(Stack.status.in_(ACTIVE_STATUSES))
        )
    ).all()
    for stack, owner_suspended in rows:
        services, volumes, endpoints = await stack_children(db, stack.id)
        if not endpoints:
            continue
        try:
            spec = spec_for(
                stack, services, volumes, endpoints, settings, owner_suspended=bool(owner_suspended)
            )
        except SpecValidationError:
            continue
        routes.extend(caddy.routes_for_stack(spec, suspended=spec.suspended))
    return routes


async def on_stack_status(db: AsyncSession, name: str, status: str, error: str | None) -> None:
    """Reconciler hook: project convergence outcomes onto the stack row.
    `observed_generation` catches up only when the stack is converged."""
    stack = (await db.execute(select(Stack).where(Stack.name == name))).scalar_one_or_none()
    if stack is None:
        return  # observed-only stack (teardown of host remnants)
    stack.status = status
    stack.error_message = error
    if status in ("ready", "suspended"):
        stack.observed_generation = stack.generation
    stack.updated_at = utcnow()
    await db.commit()


# --- create-time allocation -------------------------------------------------------


def generate_secret() -> str:
    return pysecrets.token_urlsafe(24)


async def allocate(
    db: AsyncSession,
    settings: Settings,
    blueprint: Blueprint,
    inputs,
    *,
    owner_id: int,
) -> Allocation:
    """Ports (from the shared loopback ledger) + generated secrets for one
    render. Ports are reserved for ALL declared services up front; the
    UNIQUE constraint still arbitrates concurrent racers at commit."""
    allocated: dict[str, int] = {}
    taken = await ports.used_host_ports(db)
    for service_name in blueprint.ports_needed(inputs):
        port = None
        for candidate in range(settings.app_port_min, settings.app_port_max + 1):
            if candidate not in taken:
                port = candidate
                break
        if port is None:
            raise ports.NoFreePortError("No free loopback ports left on this server")
        taken.add(port)
        allocated[service_name] = port
    secrets = {name: generate_secret() for name in blueprint.secrets_needed(inputs)}
    return Allocation(tenant=tenant_for(owner_id), ports=allocated, secrets=secrets)


async def persist_rendered(
    db: AsyncSession, stack: Stack, spec: StackSpec, settings: Settings
) -> None:
    """Write the rendered spec as child rows (env encrypted at rest)."""
    for svc in spec.services:
        db.add(
            StackService(
                stack_id=stack.id,
                name=svc.name,
                image=svc.image,
                internal_port=svc.internal_port,
                host_port=svc.host_port,
                env_encrypted=encrypt_env(dict(svc.env), settings),
                memory_mb=svc.memory_mb,
                cpu_percent=svc.cpu_percent,
                is_web=svc.is_web,
            )
        )
    for vol in spec.volumes:
        db.add(
            StackVolume(
                stack_id=stack.id,
                name=vol.name,
                service_name=vol.service,
                mount_path=vol.mount_path,
            )
        )
    for ep in spec.endpoints:
        db.add(
            StackEndpoint(
                stack_id=stack.id,
                domain=ep.domain,
                service_name=ep.service,
                behind_cloudflare=ep.behind_cloudflare,
            )
        )


def get_blueprint_or_422(blueprint_id: str) -> Blueprint:
    blueprint = get_blueprint(blueprint_id)
    if blueprint is None:
        raise StackValidationError(f"Unknown blueprint: {blueprint_id!r}")
    return blueprint
