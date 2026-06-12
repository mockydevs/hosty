"""The Action union (v2/M1, ADR-013). Actions are dumb, frozen data —
everything the executor needs is on the action; ordering is entirely the
planner's responsibility. The executor dispatches each to one adapter call
and never decides sequencing.

Conditional logic lives in exactly one action: RemoveTenantIfEmpty, where
"empty" is a host/ledger fact only the executor can see at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.specs import StackSpec


@dataclass(frozen=True)
class EnsureTenant:
    """Provision (or repair) the stack's tenant Linux user + user manager."""

    tenant: str


@dataclass(frozen=True)
class EnsureVolumeDir:
    """Create one volume directory under the tenant's home, tenant-owned."""

    tenant: str
    stack: str
    volume: str


@dataclass(frozen=True)
class WriteUnits:
    """Sync the stack's quadlet directory to EXACTLY the spec's unit set:
    write/overwrite every desired unit + env file, delete any stack-owned
    unit file not in the set. Carries the full spec — the renderer needs
    all of it."""

    stack: StackSpec


@dataclass(frozen=True)
class RemoveUnits:
    """Delete every unit + env file belonging to the stack."""

    tenant: str
    stack: str


@dataclass(frozen=True)
class DaemonReload:
    """`systemctl --user daemon-reload` in the tenant's manager."""

    tenant: str


@dataclass(frozen=True)
class StartService:
    tenant: str
    stack: str
    service: str


@dataclass(frozen=True)
class StopService:
    tenant: str
    stack: str
    service: str


@dataclass(frozen=True)
class RestartService:
    tenant: str
    stack: str
    service: str


@dataclass(frozen=True)
class RemoveVolumeDir:
    """Delete one volume directory (and its data) under the tenant's home."""

    tenant: str
    stack: str
    volume: str


@dataclass(frozen=True)
class RemoveTenantIfEmpty:
    """Tear down the tenant user IF it owns no remaining stacks — the
    executor consults the ledger; the planner cannot know."""

    tenant: str


@dataclass(frozen=True)
class SyncCaddy:
    """Rebuild + reload ingress routes from current desired state."""


Action = (
    EnsureTenant
    | EnsureVolumeDir
    | WriteUnits
    | RemoveUnits
    | DaemonReload
    | StartService
    | StopService
    | RestartService
    | RemoveVolumeDir
    | RemoveTenantIfEmpty
    | SyncCaddy
)
