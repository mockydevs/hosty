"""Stacks API (v2/M4, ADR-013): blueprint-rendered workloads — list/get/
create/delete, day-2 actions, logs. Writes are asynchronous: POST/DELETE
return 202 with an operation id the UI polls via /api/operations/{id};
the background work is one on-demand reconcile of the stack.

The blueprint runs only here (create time); the reconciler converges from
the persisted rows. Owner scoping is 404-not-403, same contract as sites
and apps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, is_admin
from app.core.errors import ConflictError, NotFoundError
from app.db.models import App, Operation, Site, Stack, StackEndpoint, User
from app.domain.validate import SpecValidationError, validate_slug
from app.orchestration.blueprints import list_blueprints
from app.orchestration.blueprints.base import ActionResult, Blueprint
from app.services import quotas
from app.services import stacks as stacks_service
from app.services.stacks import StackValidationError
from app.system import quadlet, systemd_user

log = structlog.get_logger("hosty.api.stacks")

router = APIRouter(dependencies=[Depends(get_current_user)])


class BlueprintResponse(BaseModel):
    id: str
    version: int
    category: str
    icon: str
    display_name: str
    description: str
    inputs_schema: dict[str, Any]
    actions: list[str]


class StackServiceResponse(BaseModel):
    model_config = {"from_attributes": True}

    name: str
    image: str
    image_digest: str | None
    build_repo: str | None
    build_branch: str | None
    internal_port: int | None
    host_port: int | None
    memory_mb: int | None
    cpu_percent: int | None
    is_web: bool


class StackVolumeResponse(BaseModel):
    model_config = {"from_attributes": True}

    name: str
    service_name: str
    mount_path: str


class StackEndpointResponse(BaseModel):
    model_config = {"from_attributes": True}

    domain: str
    service_name: str
    behind_cloudflare: bool


class StackResponse(BaseModel):
    id: int
    name: str
    blueprint_id: str
    blueprint_version: int
    status: str
    error_message: str | None
    generation: int
    observed_generation: int
    created_at: datetime
    services: list[StackServiceResponse]
    volumes: list[StackVolumeResponse]
    endpoints: list[StackEndpointResponse]


class CreateStackRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    blueprint_id: str = Field(min_length=1, max_length=32)
    inputs: dict[str, Any] = Field(default_factory=dict)


class DeleteStackRequest(BaseModel):
    confirm_name: str


class StackOperationAccepted(BaseModel):
    stack: StackResponse
    operation_id: int
    # Secrets generated at create time, shown exactly once (never returned
    # again; persisted only encrypted).
    show_once: dict[str, str] = Field(default_factory=dict)


class StackActionRequest(BaseModel):
    # Free-form action parameters; the blueprint's handler validates them
    # (e.g. switch_php's target version).
    params: dict[str, Any] = Field(default_factory=dict)


class StackActionResponse(BaseModel):
    ok: bool
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    show_once: dict[str, str] = Field(default_factory=dict)


class StackLogsResponse(BaseModel):
    service: str
    logs: str


def _settings(request: Request):
    return request.app.state.settings


async def stack_response(db: AsyncSession, stack: Stack) -> StackResponse:
    services, volumes, endpoints = await stacks_service.stack_children(db, stack.id)
    return StackResponse(
        id=stack.id,
        name=stack.name,
        blueprint_id=stack.blueprint_id,
        blueprint_version=stack.blueprint_version,
        status=stack.status,
        error_message=stack.error_message,
        generation=stack.generation,
        observed_generation=stack.observed_generation,
        created_at=stack.created_at,
        services=[StackServiceResponse.model_validate(s) for s in services],
        volumes=[StackVolumeResponse.model_validate(v) for v in volumes],
        endpoints=[StackEndpointResponse.model_validate(e) for e in endpoints],
    )


async def fetch_owned_stack(db: AsyncSession, user: User, stack_id: int) -> Stack:
    """Admins see everything; clients get a 404 (not 403) for other tenants'
    stacks so existence never leaks — same contract as sites and apps."""
    stack = await db.get(Stack, stack_id)
    if stack is None or (not is_admin(user) and stack.owner_id != user.id):
        raise NotFoundError("Stack not found")
    return stack


def _parse_inputs(blueprint: Blueprint, raw: dict[str, Any]) -> BaseModel:
    try:
        return blueprint.inputs().model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'inputs'}: {err['msg']}"
            for err in exc.errors()
        )
        raise StackValidationError(f"Invalid blueprint inputs — {problems}") from exc


async def _refuse_domain_conflicts(db: AsyncSession, domains: list[str]) -> None:
    """One domain, one route: collisions with sites, apps, and other stacks
    are refused at create."""
    if not domains:
        return
    if (
        await db.execute(select(StackEndpoint).where(StackEndpoint.domain.in_(domains)))
    ).first() is not None:
        raise ConflictError("Another stack already uses this domain")
    if (await db.execute(select(Site).where(Site.domain.in_(domains)))).first() is not None:
        raise ConflictError("A site already uses this domain")
    if (await db.execute(select(App).where(App.domain.in_(domains)))).first() is not None:
        raise ConflictError("An app already uses this domain")


@router.get("/blueprints", response_model=list[BlueprintResponse])
async def get_blueprints() -> Any:
    """The catalog that drives the create wizard: each blueprint's typed
    inputs as JSON Schema — no per-blueprint UI code."""
    return [
        BlueprintResponse(
            id=bp.id,
            version=bp.version,
            category=bp.category,
            icon=bp.icon,
            display_name=bp.display_name,
            description=bp.description,
            inputs_schema=bp.inputs().model_json_schema(),
            actions=sorted(bp.actions()),
        )
        for bp in list_blueprints()
    ]


@router.get("", response_model=list[StackResponse])
async def list_stacks(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> Any:
    query = select(Stack).order_by(Stack.name)
    if not is_admin(user):
        query = query.where(Stack.owner_id == user.id)
    stacks = (await db.execute(query)).scalars().all()
    return [await stack_response(db, stack) for stack in stacks]


@router.get("/{stack_id}", response_model=StackResponse)
async def get_stack(
    stack_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    return await stack_response(db, await fetch_owned_stack(db, user, stack_id))


@router.post("", response_model=StackOperationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_stack(
    request: Request,
    body: CreateStackRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    settings = _settings(request)
    blueprint = stacks_service.get_blueprint_or_422(body.blueprint_id)
    try:
        name = validate_slug(body.name, what="stack name")
    except SpecValidationError as exc:
        raise StackValidationError(str(exc)) from exc
    inputs = _parse_inputs(blueprint, body.inputs)

    limits = await quotas.effective_limits(db, user)
    if not is_admin(user) and limits.max_stacks is not None:
        owned = (
            await db.execute(
                select(func.count()).select_from(Stack).where(Stack.owner_id == user.id)
            )
        ).scalar_one()
        if owned >= limits.max_stacks:
            raise ConflictError(f"Stack quota reached ({limits.max_stacks})")

    existing = (await db.execute(select(Stack).where(Stack.name == name))).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("A stack already uses this name")

    alloc = await stacks_service.allocate(db, settings, blueprint, inputs, owner_id=user.id)
    try:
        spec = blueprint.render(name, inputs, alloc)
    except SpecValidationError as exc:
        raise StackValidationError(str(exc)) from exc
    await _refuse_domain_conflicts(db, [ep.domain for ep in spec.endpoints])

    stack = Stack(
        owner_id=user.id,
        name=name,
        blueprint_id=blueprint.id,
        blueprint_version=blueprint.version,
        inputs_encrypted=stacks_service.encrypt_inputs(body.inputs, settings),
        status="converging",
    )
    db.add(stack)
    await db.flush()
    await stacks_service.persist_rendered(db, stack, spec, settings)
    op = Operation(
        kind="create_stack",
        stack_id=stack.id,
        domain=spec.endpoints[0].domain if spec.endpoints else stack.name,
    )
    db.add(op)
    await db.commit()
    await db.refresh(stack)
    await db.refresh(op)

    reconciler = request.app.state.reconciler
    background.add_task(reconciler.converge_stack, stack.name, operation_id=op.id)
    return StackOperationAccepted(
        stack=await stack_response(db, stack), operation_id=op.id, show_once=alloc.secrets
    )


@router.delete(
    "/{stack_id}", response_model=StackOperationAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def delete_stack(
    request: Request,
    stack_id: int,
    body: DeleteStackRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    stack = await fetch_owned_stack(db, user, stack_id)
    if body.confirm_name.strip().lower() != stack.name:
        raise ConflictError("Confirmation does not match the stack name")

    # Deletion is convergence toward absence: flipping the status removes the
    # stack from desired state; the planner tears the host down and the
    # status hook drops the rows. Re-requesting a stuck delete just converges
    # again — there is nothing to undo.
    stack.status = "deleting"
    stack.generation += 1
    response = await stack_response(db, stack)
    op = Operation(kind="delete_stack", stack_id=stack.id, domain=stack.name)
    db.add(op)
    await db.commit()
    await db.refresh(op)

    reconciler = request.app.state.reconciler
    background.add_task(reconciler.converge_stack, stack.name, operation_id=op.id)
    return StackOperationAccepted(stack=response, operation_id=op.id)


@router.post("/{stack_id}/actions/{action_name}", response_model=StackActionResponse)
async def run_stack_action(
    request: Request,
    stack_id: int,
    action_name: str,
    background: BackgroundTasks,
    body: StackActionRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    stack = await fetch_owned_stack(db, user, stack_id)
    if stack.status == "deleting":
        raise ConflictError("Stack is being deleted")
    blueprint = stacks_service.get_blueprint_or_422(stack.blueprint_id)
    handler = blueprint.actions().get(action_name)
    if handler is None:
        raise NotFoundError("Action not found")
    settings = _settings(request)
    op = Operation(
        kind=f"action:{blueprint.id}.{action_name}",
        stack_id=stack.id,
        domain=stack.name,
        status="running",
    )
    db.add(op)
    await db.commit()
    try:
        result = await handler(
            db=db,
            settings=settings,
            stack=stack,
            inputs=stacks_service.decrypt_inputs(stack, settings),
            params=body.params if body is not None else {},
        )
    except Exception as exc:
        # Undo-free, like the reconciler: the action reports and the caller
        # retries — a host fault must never surface as a bare 500.
        log.error("stack_action_failed", stack=stack.name, action=action_name, error=str(exc))
        result = ActionResult(ok=False, message=str(exc)[:500])
    op.status = "succeeded" if result.ok else "failed"
    op.error = None if result.ok else (result.message[:500] or "action failed")
    await db.commit()
    # An action that edited desired state (rotate_salts, switch_php, ...)
    # bumped the generation — converge it without waiting for the interval.
    if result.ok and stack.generation > stack.observed_generation:
        background.add_task(request.app.state.reconciler.converge_stack, stack.name)
    return StackActionResponse(
        ok=result.ok, message=result.message, data=result.data, show_once=result.show_once
    )


@router.get("/{stack_id}/logs", response_model=StackLogsResponse)
async def stack_logs(
    stack_id: int,
    service: str | None = Query(default=None, max_length=32),
    tail: int = Query(default=200, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    stack = await fetch_owned_stack(db, user, stack_id)
    if stack.owner_id is None:
        raise ConflictError("Stack has no owner (account deleted); it is being torn down")
    services, _, _ = await stacks_service.stack_children(db, stack.id)
    if not services:
        raise NotFoundError("Stack has no services")
    if service is None:
        chosen = next((s for s in services if s.is_web), services[0])
    else:
        chosen = next((s for s in services if s.name == service), None)
        if chosen is None:
            raise NotFoundError("Service not found")
    logs = await systemd_user.journal(
        stacks_service.tenant_for(stack.owner_id),
        quadlet.service_unit_name(stack.name, chosen.name),
        tail=tail,
    )
    return StackLogsResponse(service=chosen.name, logs=logs)
