"""Blueprint contract + registry (v2/M4, ADR-013).

A blueprint is a typed recipe: a pydantic `inputs()` model (drives the UI
form — field types, secret flags, defaults), `render(name, tenant, inputs,
alloc) -> StackSpec`, day-2 `actions()`, optional backup hooks, and a
health projection. The blueprint runs at CREATE/UPGRADE time only — the
reconciler reads the rendered rows, never the blueprint, so a blueprint
release can never silently mutate existing stacks (`blueprint_version` is
recorded on the stack; re-render happens only on an explicit upgrade).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from app.domain.specs import StackSpec


@dataclass(frozen=True)
class Allocation:
    """Panel-allocated resources the blueprint may consume while rendering.

    `ports`: service name -> loopback host port (allocated for every service
    the blueprint DECLARED in `ports_needed`). `secrets`: generated secret
    values keyed by the blueprint's declared secret names — shown once to
    the user, then only stored encrypted."""

    tenant: str
    ports: dict[str, int] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    """Typed envelope for day-2 action responses. `show_once` carries
    secrets the UI must display exactly once (admin passwords, login
    links); it is never persisted."""

    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    show_once: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StackHealth:
    healthy: bool
    detail: str = ""


# Handlers are invoked with keyword arguments: db (AsyncSession), settings,
# stack (the ORM row), inputs (decrypted blueprint inputs dict) and params
# (the request's optional JSON body, validated by the blueprint).
ActionHandler = Callable[..., Awaitable[ActionResult]]

# Backup hooks are invoked with keyword arguments: db, settings, stack and
# directory (the staging dir during backup / the verified backup dir during
# restore). `pre_backup` runs BEFORE volume archiving (write DB dumps into
# the staging dir); `post_restore` runs AFTER volumes are restored and the
# stack's services are running again (re-import the dumps).
BackupHook = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class BackupHooks:
    pre_backup: BackupHook
    post_restore: BackupHook
    # Database names recorded in the ADR-010 manifest (informational).
    databases: tuple[str, ...] = ()


@runtime_checkable
class Blueprint(Protocol):
    id: str
    version: int

    def inputs(self) -> type[BaseModel]:
        """Pydantic model describing the user-supplied inputs. Field
        json_schema_extra={"secret": True} marks write-only secret fields;
        the schema drives the create wizard — no per-blueprint UI code."""
        ...

    def ports_needed(self, inputs: BaseModel) -> list[str]:
        """Service names that publish a loopback port (allocated by panel)."""
        ...

    def secrets_needed(self, inputs: BaseModel) -> list[str]:
        """Names of secrets the panel must generate before render."""
        ...

    def render(self, name: str, inputs: BaseModel, alloc: Allocation) -> StackSpec:
        """Pure: inputs + allocation -> complete StackSpec. Raises
        SpecValidationError for anything the grammar rejects."""
        ...

    def actions(self) -> dict[str, ActionHandler]:
        """Day-2 operations exposed at POST /stacks/{id}/actions/{name}."""
        ...

    def backup_hooks(self) -> BackupHooks | None:
        """Blueprint participation in stack backups (None = volumes only)."""
        ...

    def health(self, observed_active: dict[str, bool]) -> StackHealth:
        """Project observed service activity into one health verdict."""
        ...


_REGISTRY: dict[str, Blueprint] = {}


def register(blueprint: Blueprint) -> None:
    if blueprint.id in _REGISTRY:
        raise ValueError(f"Blueprint {blueprint.id!r} already registered")
    _REGISTRY[blueprint.id] = blueprint


def get_blueprint(blueprint_id: str) -> Blueprint | None:
    return _REGISTRY.get(blueprint_id)


def list_blueprints() -> list[Blueprint]:
    return sorted(_REGISTRY.values(), key=lambda b: b.id)
