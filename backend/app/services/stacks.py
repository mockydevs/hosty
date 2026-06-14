"""Stacks business layer (v2/M4, ADR-013): the bridge between DB rows
(desired state, encrypted at rest) and the pure domain specs the planner
consumes. Also the reconciler's wiring: desired-state loader, Caddy route
assembly, and the status projection hook.

The blueprint runs ONLY here, at create/upgrade time, persisting rendered
rows. The reconciler reads rows, never the blueprint.
"""

from __future__ import annotations

import dataclasses
import json
import secrets as pysecrets
from urllib.parse import quote

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import AppError
from app.core.secrets import decrypt_secret, encrypt_secret
from app.db.models import Stack, StackEndpoint, StackService, StackVolume, User
from app.domain.specs import EndpointSpec, ServiceSpec, StackSpec, VolumeSpec, derive_host_port
from app.domain.validate import SpecValidationError
from app.orchestration.blueprints import get_blueprint
from app.orchestration.blueprints.base import Allocation, Blueprint
from app.services import caddy, ports, stack_images

log = structlog.get_logger("hosty.stacks")

# Stacks being deleted leave desired state; every other status converges.
ACTIVE_STATUSES = ("converging", "ready", "degraded", "suspended", "error")


class StackValidationError(AppError):
    status_code = 422
    code = "invalid_stack"


def tenant_for(owner_id: int) -> str:
    return f"hosty-t-{int(owner_id)}"


def suggested_domain(name: str, settings: Settings) -> str | None:
    """Auto-generated public domain for a stack. Uses the configured wildcard
    base (`<name>.<apps_base_domain>`) when set; otherwise a zero-config
    sslip.io name off the server's public IP (`<name>.<ip>.sslip.io`), which
    resolves to the box with no DNS setup. None when neither is available."""
    base = (settings.apps_base_domain or "").strip().lower().strip(".")
    if base:
        return f"{name}.{base}"
    ip = (settings.public_ip or "").strip()
    if ip:
        return f"{name}.{ip}.sslip.io"
    return None


def with_auto_domain(spec: StackSpec, settings: Settings) -> StackSpec:
    """If a web-facing stack was created without a domain, attach a generated
    one so it is reachable by default (editable later). No-op when the spec
    already has an endpoint, has no public web service, or no domain can be
    generated."""
    if spec.endpoints:
        return spec
    web = next((s for s in spec.services if s.is_web and s.internal_port is not None), None)
    if web is None:
        return spec
    domain = suggested_domain(spec.name, settings)
    if domain is None:
        return spec
    return dataclasses.replace(spec, endpoints=(EndpointSpec(domain=domain, service=web.name),))


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
        loopback_ip=stack.loopback_ip,
        server_id=stack.server_id,
        services=tuple(
            ServiceSpec(
                name=svc.name,
                image=svc.image_digest or svc.image,
                env=tuple(sorted(decrypt_env(svc, settings).items())),
                internal_port=svc.internal_port,
                memory_mb=svc.memory_mb,
                cpu_percent=svc.cpu_percent,
                is_web=svc.is_web,
                build_repo=svc.build_repo,
                build_branch=svc.build_branch,
                exposed=svc.publicly_exposed,
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
    `observed_generation` catches up only when the stack is converged.
    `absent` (host fully torn down, stack not in desired state) finalizes a
    deletion by removing the rows — and ONLY a deletion: a stack excluded
    from desired state for any other reason keeps its row."""
    stack = (await db.execute(select(Stack).where(Stack.name == name))).scalar_one_or_none()
    if stack is None:
        return  # observed-only stack (teardown of host remnants)
    if status == "absent":
        if stack.status == "deleting":
            await db.delete(stack)
            await db.commit()
        return
    stack.status = status
    stack.error_message = error
    if status in ("ready", "suspended"):
        stack.observed_generation = stack.generation
    stack.updated_at = utcnow()
    await db.commit()


async def sync_ingress(db: AsyncSession, settings: Settings, *, client=None) -> None:
    """Rebuild + apply the FULL Caddy config (sites + apps + stacks). The
    executor's SyncCaddy action lands here; ownership of build_full_config
    moves to services/ingress.py at M6."""
    from app.services.sites import _caddy_client, build_full_config

    await _caddy_client(settings, client).apply(await build_full_config(db, settings))


def build_reconciler(sessionmaker, settings: Settings, *, observe=None, caddy_client=None):
    """The fully wired reconciler: DB-backed desired state, full ingress
    sync, and status projection. `observe`/`caddy_client` are injectable for
    tests (FakeHost / fake Caddy admin)."""
    from app.orchestration.reconciler import Reconciler

    async def _load(db: AsyncSession):
        return await load_desired(db, settings)

    async def _sync(db: AsyncSession) -> None:
        await sync_ingress(db, settings, client=caddy_client)

    kwargs = {} if observe is None else {"observe": observe}
    return Reconciler(
        sessionmaker,
        settings,
        load_desired=_load,
        sync_caddy=_sync,
        on_stack_status=on_stack_status,
        **kwargs,
    )


# --- create-time allocation -------------------------------------------------------


def generate_secret() -> str:
    return pysecrets.token_urlsafe(24)


async def allocate(
    db: AsyncSession,
    settings: Settings,
    blueprint: Blueprint,
    inputs,
    *,
    stack: Stack,
) -> Allocation:
    """Generated secrets and loopback IP for one render."""
    secrets = {name: generate_secret() for name in blueprint.secrets_needed(inputs)}
    return Allocation(tenant=tenant_for(stack.owner_id), loopback_ip=stack.loopback_ip, secrets=secrets)


async def persist_rendered(
    db: AsyncSession, stack: Stack, spec: StackSpec, settings: Settings
) -> None:
    """Write the rendered spec as child rows (env encrypted at rest)."""
    image_locks = await stack_images.resolve_stack_image_locks(spec)
    for svc in spec.services:
        db.add(
            StackService(
                stack_id=stack.id,
                name=svc.name,
                image=svc.image,
                image_digest=image_locks.get(svc.name),
                build_repo=svc.build_repo,
                build_branch=svc.build_branch,
                internal_port=svc.internal_port,
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


@dataclasses.dataclass(frozen=True)
class ConnectionLink:
    """A copy-paste connection URI for a stack's DB service, at each scope.
    `public_uri` is present only when the service is publicly exposed AND a
    public IP is configured."""

    service: str
    scheme: str
    exposed: bool
    internal_uri: str  # other containers on the stack network
    host_uri: str  # code on this server / other stacks (loopback)
    public_uri: str | None  # from anywhere (server public IP), when exposed


def _build_uri(scheme: str, user: str, password: str, host: str, port: int, database: str) -> str:
    if user:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    elif password:
        auth = f":{quote(password, safe='')}@"
    else:
        auth = ""
    path = f"/{quote(database, safe='')}" if database else ""
    return f"{scheme}://{auth}{host}:{port}{path}"


def connection_links(
    stack: Stack, services: list[StackService], settings: Settings
) -> list[ConnectionLink]:
    """Connection URIs for a stack's DB service, derived from the blueprint's
    `connection` metadata + the service's resolved env. Empty when the
    blueprint declares no connection or the service publishes no port."""
    blueprint = get_blueprint(stack.blueprint_id)
    meta = getattr(blueprint, "connection_meta", lambda: None)()
    if not meta:
        return []
    target = next((s for s in services if s.name == meta.get("service")), None)
    if target is None or target.internal_port is None:
        return []
    env = decrypt_env(target, settings)
    scheme = str(meta["scheme"])
    user = env.get(meta.get("user_env", ""), "")
    password = env.get(meta.get("password_env", ""), "")
    database = env.get(meta.get("database_env", ""), "")
    public_ip = (settings.public_ip or "").strip()
    return [
        ConnectionLink(
            service=target.name,
            scheme=scheme,
            exposed=target.publicly_exposed,
            internal_uri=_build_uri(
                scheme,
                user,
                password,
                f"{stack.name}-{target.name}",
                target.internal_port,
                database,
            ),
            host_uri=_build_uri(
                scheme, user, password, stack.loopback_ip, derive_host_port(target.internal_port), database
            ),
            public_uri=(
                _build_uri(
                    scheme, user, password, public_ip, derive_host_port(target.internal_port), database
                )
                if target.publicly_exposed and public_ip
                else None
            ),
        )
    ]


def get_blueprint_or_422(blueprint_id: str) -> Blueprint:
    blueprint = get_blueprint(blueprint_id)
    if blueprint is None:
        raise StackValidationError(f"Unknown blueprint: {blueprint_id!r}")
    return blueprint
