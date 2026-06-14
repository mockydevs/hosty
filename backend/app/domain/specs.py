"""Desired- and observed-state specs (v2/M1, ADR-013). Pure, frozen,
ORM-free. Construction IS validation: an instance that exists is well-formed
(`__post_init__` enforces the grammar and cross-references), so downstream
code never re-checks.

`spec_hash` is the convergence fingerprint: adapters embed it in generated
unit files (`# hosty-spec-hash=<hash>`), observers read it back, and the
planner compares. Unit content changes if and only if the hash changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.domain.validate import (
    SpecValidationError,
    validate_domain_name,
    validate_env_key,
    validate_env_value,
    validate_git_ref,
    validate_git_repo,
    validate_image_ref,
    validate_mount_path,
    validate_port,
    validate_slug,
)

TENANT_RE_HINT = "hosty-t-<id>"


@dataclass(frozen=True)
class VolumeSpec:
    """A persistent directory, bound into exactly one service."""

    name: str
    service: str
    mount_path: str

    def __post_init__(self) -> None:
        validate_slug(self.name, what="volume name")
        validate_slug(self.service, what="service reference")
        validate_mount_path(self.mount_path)


@dataclass(frozen=True)
class ServiceSpec:
    """One container. `env` is a sorted tuple of pairs (hashable, canonical);
    encryption is a storage concern that never reaches the domain."""

    name: str
    image: str
    env: tuple[tuple[str, str], ...] = ()
    internal_port: int | None = None
    host_port: int | None = None  # panel-allocated loopback port
    memory_mb: int | None = None
    cpu_percent: int | None = None
    is_web: bool = False
    build_repo: str | None = None
    build_branch: str | None = None
    build_tool: str = "dockerfile"
    # When True the published port binds 0.0.0.0 (reachable on the server's
    # public IP) instead of loopback-only — opt-in external access for e.g. a
    # database. Requires a published port.
    exposed: bool = False

    def __post_init__(self) -> None:
        validate_slug(self.name, what="service name")
        if self.exposed and self.internal_port is None:
            raise SpecValidationError(
                f"Service {self.name!r}: cannot expose a service that publishes no port"
            )
        if self.build_repo:
            validate_git_repo(self.build_repo)
            if self.build_branch:
                validate_git_ref(self.build_branch)
        else:
            # validate_image_ref validates AND qualifies (e.g. redis:7.2 →
            # docker.io/library/redis:7.2); frozen dataclass needs __setattr__.
            object.__setattr__(self, "image", validate_image_ref(self.image))
        for key, value in self.env:
            validate_env_key(key)
            validate_env_value(key, value)
        if list(self.env) != sorted(self.env):
            raise SpecValidationError("Service env must be sorted (canonical form)")
        if self.internal_port is not None:
            validate_port(self.internal_port)

        if self.memory_mb is not None and not 16 <= int(self.memory_mb) <= 1_048_576:
            raise SpecValidationError(f"Service {self.name!r}: invalid memory_mb")
        if self.cpu_percent is not None and not 1 <= int(self.cpu_percent) <= 6400:
            raise SpecValidationError(f"Service {self.name!r}: invalid cpu_percent")


@dataclass(frozen=True)
class EndpointSpec:
    """domain -> service's published loopback port, through Caddy."""

    domain: str
    service: str
    behind_cloudflare: bool = False

    def __post_init__(self) -> None:
        validate_domain_name(self.domain)
        validate_slug(self.service, what="service reference")


@dataclass(frozen=True)
class StackSpec:
    """One deployable unit a client owns. Cross-references are verified at
    construction: a StackSpec that exists is internally consistent."""

    name: str
    tenant: str  # tenant linux user (hosty-t-<id>)
    loopback_ip: str  # e.g., 127.1.0.1
    services: tuple[ServiceSpec, ...]
    volumes: tuple[VolumeSpec, ...] = ()
    endpoints: tuple[EndpointSpec, ...] = ()
    suspended: bool = False
    server_id: int | None = None  # None = localhost (default)

    def __post_init__(self) -> None:
        validate_slug(self.name, what="stack name")
        if not (
            isinstance(self.tenant, str)
            and self.tenant.startswith("hosty-t-")
            and self.tenant[len("hosty-t-") :].isdigit()
        ):
            raise SpecValidationError(f"Invalid tenant {self.tenant!r} (must be {TENANT_RE_HINT})")
        if not self.services:
            raise SpecValidationError(f"Stack {self.name!r} has no services")
        service_names = [s.name for s in self.services]
        if len(set(service_names)) != len(service_names):
            raise SpecValidationError(f"Stack {self.name!r}: duplicate service names")
        volume_defs: set[tuple[str, str]] = set()
        known = set(service_names)
        for volume in self.volumes:
            volume_key = (volume.name, volume.service)
            if volume_key in volume_defs:
                raise SpecValidationError(
                    f"Stack {self.name!r}: duplicate volume {volume.name!r} mounts into "
                    f"service {volume.service!r} more than once"
                )
            volume_defs.add(volume_key)
            if volume.service not in known:
                raise SpecValidationError(
                    f"Stack {self.name!r}: volume {volume.name!r} mounts into "
                    f"unknown service {volume.service!r}"
                )
        endpoint_domains = [e.domain for e in self.endpoints]
        if len(set(endpoint_domains)) != len(endpoint_domains):
            raise SpecValidationError(f"Stack {self.name!r}: duplicate endpoint domains")
        by_name = {s.name: s for s in self.services}
        for endpoint in self.endpoints:
            if endpoint.service not in known:
                raise SpecValidationError(
                    f"Endpoint {endpoint.domain!r} targets missing service {endpoint.service!r}"
                )
            target = next(s for s in self.services if s.name == endpoint.service)
            if target.internal_port is None:
                raise SpecValidationError(
                    f"Endpoint {endpoint.domain!r} targets service {endpoint.service!r} which has no internal_port"
                )

    @property
    def network(self) -> str:
        return f"hosty-{self.name}"

    def unit_base(self, service: str) -> str:
        """Quadlet file base name: <base>.container -> <base>.service."""
        return f"{self.name}-{service}"

    def volumes_for(self, service: str) -> tuple[VolumeSpec, ...]:
        return tuple(v for v in self.volumes if v.service == service)


def spec_hash(stack: StackSpec, service: ServiceSpec) -> str:
    """Convergence fingerprint of one service's runtime shape. Everything
    that changes the generated unit/env files MUST be in here; anything that
    does not (endpoint domains — they only touch Caddy) MUST NOT."""
    payload = {
        "image": service.image,
        "env": list(service.env),
        "internal_port": service.internal_port,
        "exposed": service.exposed,  # flips the PublishPort bind → unit changes
        "memory_mb": service.memory_mb,
        "cpu_percent": service.cpu_percent,
        "build_repo": service.build_repo,
        "build_branch": service.build_branch,
        "build_tool": service.build_tool,
        "loopback_ip": stack.loopback_ip,  # changing IP rewrites PublishPort bind
        "network": stack.network,
        "volumes": [(v.name, v.mount_path) for v in stack.volumes_for(service.name)],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


# --- observed state ---------------------------------------------------------------


@dataclass(frozen=True)
class ObservedUnit:
    """One service unit as the host reports it. `spec_hash` is read from the
    `# hosty-spec-hash=` marker the adapter embeds; None means the file is
    missing the marker (foreign/corrupt) and therefore always stale."""

    spec_hash: str | None
    active: bool


@dataclass(frozen=True)
class ObservedStack:
    """Host-side artifacts attributed to one stack name. Observers only
    report stacks that left ANY artifact; a fully absent stack has no entry."""

    tenant: str
    tenant_present: bool = True
    units: dict[str, ObservedUnit] = field(default_factory=dict)  # service -> unit
    build_units: dict[str, str | None] = field(default_factory=dict)  # service -> spec hash
    volume_dirs: frozenset[str] = frozenset()


# Observed world: stack name -> ObservedStack
Observed = dict[str, ObservedStack]

def derive_host_port(internal_port: int) -> int:
    return internal_port + 20000 if internal_port < 1024 else internal_port
