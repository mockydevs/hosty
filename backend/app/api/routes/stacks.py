"""Stacks API (v2/M4, ADR-013): blueprint-rendered workloads — list/get/
create/delete, day-2 actions, logs. Writes are asynchronous: POST/DELETE
return 202 with an operation id the UI polls via /api/operations/{id};
the background work is one on-demand reconcile of the stack.

The blueprint runs only here (create time); the reconciler converges from
the persisted rows. Owner scoping is 404-not-403, same contract as sites
and apps.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from app.api.routes.servers import ServerResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, is_admin
from app.core.errors import ConflictError, NotFoundError
from app.core.secrets import SecretDecryptionError, decrypt_secret
from app.db.models import App, GitSource, Operation, Site, Stack, StackEndpoint, User, ScheduledTask, Tag, StackTag, Deployment
from app.domain.validate import SpecValidationError, validate_domain_name, validate_slug
from app.orchestration.blueprints import list_blueprints
from app.orchestration.blueprints.base import ActionResult, Blueprint
from app.services import image_versions, quotas, tenancy, git
from app.services.github import authenticated_clone_url, get_installation_token
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
    memory_mb: int | None
    cpu_percent: int | None
    is_web: bool
    publicly_exposed: bool
    post_start_command: str | None


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
    inputs: dict[str, Any]
    server_id: int | None = None
    server: ServerResponse | None = None


class CreateStackRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    blueprint_id: str = Field(min_length=1, max_length=32)
    inputs: dict[str, Any] = Field(default_factory=dict)
    server_id: int | None = None
    server: ServerResponse | None = None  # None = deploy to localhost

class SetStackEnvRequest(BaseModel):
    env: dict[str, str]


class DeleteStackRequest(BaseModel):
    confirm_name: str


class StackOperationAccepted(BaseModel):
    stack: StackResponse
    operation_id: int
    # Secrets generated at create time, shown exactly once (never returned
    # again; persisted only encrypted).
    show_once: dict[str, str] = Field(default_factory=dict)
    # Non-fatal advisory messages (e.g. PORT mismatch, magic var conflicts).
    warnings: list[str] = Field(default_factory=list)


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


class GitAnalyzeRequest(BaseModel):
    repo: str
    branch: str
    source_id: int | None = None


class GitAnalyzeResponse(BaseModel):
    has_dockerfile: bool
    has_compose: bool
    compose_services: list[str]
    env_keys: list[str]
    compose_file_content: str | None = None


class ConnectionLinkResponse(BaseModel):
    service: str
    scheme: str
    exposed: bool
    internal_uri: str  # other containers on the stack network
    host_uri: str  # code on this server / other stacks (loopback)
    public_uri: str | None  # from anywhere, when exposed + a public IP is set


class ExposeServiceRequest(BaseModel):
    exposed: bool


def _settings(request: Request):
    return request.app.state.settings


async def stack_response(db: AsyncSession, stack: Stack, request: Request) -> StackResponse:
    services, volumes, endpoints = await stacks_service.stack_children(db, stack.id)
    try:
        inputs = stacks_service.decrypt_inputs(stack, request.app.state.settings)
    except SecretDecryptionError:
        log.warning("stack_inputs_decrypt_failed", stack_id=stack.id)
        inputs = {}
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
        inputs=inputs,
        server_id=stack.server_id,
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


async def _refuse_domain_conflicts(
    db: AsyncSession, domains: list[str], *, exclude_stack_id: int | None = None
) -> None:
    """One domain, one route: collisions with sites, apps, and other stacks
    are refused. `exclude_stack_id` skips the stack's own endpoints so a
    domain edit that keeps the same value is not a self-conflict."""
    if not domains:
        return
    stack_q = select(StackEndpoint).where(StackEndpoint.domain.in_(domains))
    if exclude_stack_id is not None:
        stack_q = stack_q.where(StackEndpoint.stack_id != exclude_stack_id)
    if (await db.execute(stack_q)).first() is not None:
        raise ConflictError("Another stack already uses this domain")
    if (await db.execute(select(Site).where(Site.domain.in_(domains)))).first() is not None:
        raise ConflictError("A site already uses this domain")
    if (await db.execute(select(App).where(App.domain.in_(domains)))).first() is not None:
        raise ConflictError("An app already uses this domain")


async def _inputs_schema_with_versions(bp: Blueprint) -> dict[str, Any]:
    """The blueprint's JSON Schema, with any registry-backed version field
    turned into an enum (newest series first) so the wizard renders a
    dropdown instead of free text. Falls back to the static schema for
    blueprints that declare no dynamic versions."""
    schema = bp.inputs().model_json_schema()
    version_inputs = getattr(bp, "version_inputs", None)
    if version_inputs is None:
        return schema
    props = schema.get("properties", {})
    for field_name, (repo, default) in version_inputs().items():
        prop = props.get(field_name)
        if not isinstance(prop, dict):
            continue
        series = await image_versions.available_series(repo, default=default or "latest")
        prop["enum"] = series
        if series:
            prop["default"] = series[0]  # genuine latest when online; template default offline
    return schema


@router.get("/blueprints", response_model=list[BlueprintResponse])
async def get_blueprints() -> Any:
    """The catalog that drives the create wizard: each blueprint's typed
    inputs as JSON Schema — no per-blueprint UI code. Version fields are
    populated from the image registry so new releases appear automatically."""
    blueprints = list_blueprints()
    schemas = await asyncio.gather(*(_inputs_schema_with_versions(bp) for bp in blueprints))
    return [
        BlueprintResponse(
            id=bp.id,
            version=bp.version,
            category=bp.category,
            icon=bp.icon,
            display_name=bp.display_name,
            description=bp.description,
            inputs_schema=schema,
            actions=sorted(bp.actions()),
        )
        for bp, schema in zip(blueprints, schemas, strict=True)
    ]


@router.post("/git/analyze", response_model=GitAnalyzeResponse)
async def analyze_git_repo(
    body: GitAnalyzeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitAnalyzeResponse:
    clone_url = body.repo
    if body.source_id is not None:
        source = await db.get(GitSource, body.source_id)
        if source and source.owner_id == user.id and source.installation_id:
            try:
                token = await get_installation_token(source, request.app.state.settings)
                clone_url = authenticated_clone_url(body.repo, token)
            except Exception:
                pass  # fall through to unauthenticated clone; will fail with a clear error
    try:
        result = await git.analyze_repo(clone_url, body.branch)
        return GitAnalyzeResponse(
            has_dockerfile=result.has_dockerfile,
            has_compose=result.has_compose,
            compose_services=result.compose_services,
            env_keys=result.env_keys,
            compose_file_content=result.compose_file_content,
        )
    except ValueError as e:
        raise StackValidationError(str(e))


@router.post("/webhooks/{stack_id}", status_code=status.HTTP_202_ACCEPTED)
async def stack_webhook(
    stack_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    stack = await db.get(Stack, stack_id)
    if stack is None or stack.status == "deleting":
        raise NotFoundError("Stack not found")

    if stack.blueprint_id != "git":
        raise ConflictError("Webhook only supported for git blueprint")

    settings = request.app.state.settings
    inputs = stacks_service.decrypt_inputs(stack, settings)

    if not inputs.get("source_id"):
        token = request.headers.get("X-Hosty-Token", "")
        webhook_secret = inputs.get("webhook_secret", "")
        if not webhook_secret or not hmac.compare_digest(token, webhook_secret):
            raise NotFoundError("Stack not found")  # 404 to avoid enumeration

    if inputs.get("source_id"):
        source = await db.get(GitSource, inputs["source_id"])
        if source:
            webhook_secret = decrypt_secret(source.webhook_secret_encrypted, settings.secret_key)
            signature_header = request.headers.get("x-hub-signature-256")
            if not signature_header:
                raise ConflictError("Missing signature header")

            payload = await request.body()
            expected = "sha256=" + hmac.new(
                webhook_secret.encode(), payload, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, signature_header):
                raise ConflictError("Invalid webhook signature")

    stack.generation += 1
    op = Operation(kind="webhook_rebuild", stack_id=stack.id, domain=stack.name)
    db.add(op)
    await db.commit()
    await db.refresh(op)

    request.app.state.reconciler.enqueue(stack.name, op.id)
    return {"status": "accepted", "operation_id": op.id}


class SuggestedDomainResponse(BaseModel):
    domain: str


@router.get("/suggested-domain", response_model=SuggestedDomainResponse)
async def get_suggested_domain(
    request: Request, name: str = Query(min_length=1, max_length=32)
) -> Any:
    """Powers the create wizard's Autogenerate button: the domain a stack of
    this name would get (wildcard base, or sslip.io off the public IP)."""
    try:
        slug = validate_slug(name, what="stack name")
    except SpecValidationError as exc:
        raise StackValidationError(str(exc)) from exc
    domain = stacks_service.suggested_domain(slug, _settings(request))
    if domain is None:
        raise ConflictError(
            "No domain can be generated yet — set an apps base domain in Settings, "
            "or configure the server's public IP."
        )
    return SuggestedDomainResponse(domain=domain)


@router.get("", response_model=list[StackResponse])
async def list_stacks(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Any:
    query = select(Stack).order_by(Stack.name).limit(limit).offset(offset)
    if not is_admin(user):
        query = query.where(Stack.owner_id == user.id)
    stacks = (await db.execute(query)).scalars().all()
    if not stacks:
        return []
    svc_map, vol_map, ep_map = await stacks_service._bulk_children(db, [s.id for s in stacks])
    settings = _settings(request)
    result = []
    for stack in stacks:
        try:
            inputs = stacks_service.decrypt_inputs(stack, settings)
        except SecretDecryptionError:
            log.warning("stack_inputs_decrypt_failed", stack_id=stack.id)
            inputs = {}
        result.append(
            StackResponse(
                id=stack.id,
                name=stack.name,
                blueprint_id=stack.blueprint_id,
                blueprint_version=stack.blueprint_version,
                status=stack.status,
                error_message=stack.error_message,
                generation=stack.generation,
                observed_generation=stack.observed_generation,
                created_at=stack.created_at,
                services=[StackServiceResponse.model_validate(s) for s in svc_map[stack.id]],
                volumes=[StackVolumeResponse.model_validate(v) for v in vol_map[stack.id]],
                endpoints=[StackEndpointResponse.model_validate(e) for e in ep_map[stack.id]],
                inputs=inputs,
                server_id=stack.server_id,
            )
        )
    return result


@router.get("/{stack_id}", response_model=StackResponse)
async def stack_get(
    request: Request,
    stack_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    return await stack_response(db, await fetch_owned_stack(db, user, stack_id), request)


@router.post("", response_model=StackOperationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_stack(
    request: Request,
    body: CreateStackRequest,
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

    stack = Stack(
        owner_id=user.id,
        server_id=body.server_id,
        name=name,
        blueprint_id=blueprint.id,
        blueprint_version=blueprint.version,
        inputs_encrypted=stacks_service.encrypt_inputs(body.inputs, settings),
        status="converging",
    )
    db.add(stack)
    await db.flush()

    alloc = await stacks_service.allocate(db, settings, blueprint, inputs, stack=stack)
    try:
        spec = blueprint.render(name, inputs, alloc)
        # Web stacks created without a domain get a generated one (wildcard
        # base or sslip.io) so they are reachable by default; editable later.
        spec = stacks_service.with_auto_domain(spec, settings)
    except SpecValidationError as exc:
        raise StackValidationError(str(exc)) from exc
    await _refuse_domain_conflicts(db, [ep.domain for ep in spec.endpoints])

    await stacks_service.persist_rendered(db, stack, spec, settings)
    op = Operation(
        kind="create_stack",
        stack_id=stack.id,
        domain=spec.endpoints[0].domain if spec.endpoints else stack.name,
    )
    db.add(op)
    try:
        await db.commit()
    except IntegrityError:
        raise ConflictError("A stack with this name already exists")
    await db.refresh(stack)
    await db.refresh(op)

    reconciler = request.app.state.reconciler
    reconciler.enqueue(stack.name, op.id)
    return StackOperationAccepted(
        stack=await stack_response(db, stack, request), operation_id=op.id, show_once=alloc.secrets
    )


@router.delete(
    "/{stack_id}", response_model=StackOperationAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def delete_stack(
    request: Request,
    stack_id: int,
    body: DeleteStackRequest,
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
    response = await stack_response(db, stack, request)
    op = Operation(kind="delete_stack", stack_id=stack.id, domain=stack.name)
    db.add(op)
    await db.commit()
    await db.refresh(op)

    reconciler = request.app.state.reconciler
    reconciler.enqueue(stack.name, op.id)
    return StackOperationAccepted(stack=response, operation_id=op.id)


class SetStackDomainRequest(BaseModel):
    # Blank/omitted → generate one (wildcard base or sslip.io).
    domain: str | None = Field(default=None, max_length=253)
    behind_cloudflare: bool = False
    # Which service to route to. Omit to use the first web-facing service.
    service_name: str | None = None


@router.put(
    "/{stack_id}/domain",
    response_model=StackOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def set_stack_domain(
    request: Request,
    stack_id: int,
    body: SetStackDomainRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Set or change the public domain of a web-facing stack. A blank domain
    auto-generates one. The single web endpoint is replaced, the generation is
    bumped, and the stack re-converges (Caddy re-syncs the route)."""
    stack = await fetch_owned_stack(db, user, stack_id)
    if stack.status == "deleting":
        raise ConflictError("Stack is being deleted")
    settings = _settings(request)
    services, _, endpoints = await stacks_service.stack_children(db, stack.id)
    port_services = [s for s in services if s.internal_port is not None]
    if not port_services:
        raise StackValidationError("This stack has no web-facing service to route a domain to")

    # Resolve target service: explicit name > first is_web > first with port
    if body.service_name:
        web = next((s for s in port_services if s.name == body.service_name), None)
        if web is None:
            raise StackValidationError(f"Service {body.service_name!r} not found or has no port")
    else:
        web = next((s for s in port_services if s.is_web), None) or port_services[0]

    # Suggested domain uses stack name for primary service, stack-service for others
    slug = stack.name if web.is_web or len(port_services) == 1 else f"{stack.name}-{web.name}"
    raw = (body.domain or "").strip().lower()
    domain = raw or stacks_service.suggested_domain(slug, settings)
    if not domain:
        raise ConflictError(
            "No domain provided and none can be generated — set an apps base domain "
            "in Settings, or configure the server's public IP."
        )
    try:
        validate_domain_name(domain)
    except SpecValidationError as exc:
        raise StackValidationError(str(exc)) from exc
    await _refuse_domain_conflicts(db, [domain], exclude_stack_id=stack.id)

    # Replace only the endpoint for this service; leave other services' endpoints intact
    old_domain = next((ep.domain for ep in endpoints if ep.service_name == web.name), None)
    for endpoint in endpoints:
        if endpoint.service_name == web.name:
            await db.delete(endpoint)
    db.add(
        StackEndpoint(
            stack_id=stack.id,
            domain=domain,
            service_name=web.name,
            behind_cloudflare=body.behind_cloudflare,
        )
    )

    # Direction 1 bi-directional sync: update env vars that pointed to old domain.
    # Fallback (update any FQDN key) is safe only for single-service stacks where
    # there is no ambiguity about which service's URL to update.
    inputs = stacks_service.decrypt_inputs(stack, settings)
    updated_inputs, env_changed = stacks_service.update_env_for_domain(
        inputs, domain, old_domain, allow_fallback=len(port_services) == 1
    )
    if env_changed:
        stack.inputs_encrypted = stacks_service.encrypt_inputs(updated_inputs, settings)

    stack.generation += 1
    op = Operation(kind="converge_stack", stack_id=stack.id, domain=domain)
    db.add(op)
    await db.commit()
    await db.refresh(op)

    reconciler = request.app.state.reconciler
    reconciler.enqueue(stack.name, op.id)
    # A domain change alters no units, so converge plans nothing and would not
    # touch Caddy — re-sync ingress explicitly so the new route goes live.
    asyncio.create_task(reconciler.resync_ingress())
    return StackOperationAccepted(stack=await stack_response(db, stack, request), operation_id=op.id)


@router.put("/{stack_id}/env", response_model=StackOperationAccepted)
async def set_stack_env(
    request: Request,
    stack_id: int,
    body: SetStackEnvRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    stack = await fetch_owned_stack(db, user, stack_id)
    if stack.status == "deleting":
        raise ConflictError("Stack is being deleted")

    settings = request.app.state.settings
    inputs = stacks_service.decrypt_inputs(stack, settings)
    old_env: dict[str, str] = inputs.get("env") or {}

    inputs["env"] = body.env
    stack.inputs_encrypted = stacks_service.encrypt_inputs(inputs, settings)
    stack.generation += 1

    # Direction 2 bi-directional sync: update endpoints when FQDN env vars change
    services, _, endpoints = await stacks_service.stack_children(db, stack.id)
    to_delete, to_add_data = stacks_service.sync_endpoints_from_env_change(
        old_env, body.env, services, endpoints, stack.id
    )
    for ep in to_delete:
        await db.delete(ep)
    for data in to_add_data:
        db.add(StackEndpoint(**data))
    domain_synced = bool(to_delete or to_add_data)

    # PORT mismatch advisory: warn if PORT env var doesn't match internal_port
    warnings: list[str] = []
    port_raw = body.env.get("PORT", "").strip()
    if port_raw.isdigit():
        env_port = int(port_raw)
        for svc in services:
            if svc.internal_port is not None and svc.internal_port != env_port:
                warnings.append(
                    f'PORT={env_port} in env vars does not match service "{svc.name}" '
                    f"internal_port={svc.internal_port} — this may cause 502 Bad Gateway errors"
                )

    op = Operation(kind="converge_stack", stack_id=stack.id, domain=stack.name)
    db.add(op)
    await db.commit()
    await db.refresh(op)

    reconciler = request.app.state.reconciler
    reconciler.enqueue(stack.name, op.id)
    if domain_synced:
        asyncio.create_task(reconciler.resync_ingress())
    return StackOperationAccepted(
        stack=await stack_response(db, stack, request),
        operation_id=op.id,
        warnings=warnings,
    )


@router.post("/{stack_id}/actions/{action_name}", response_model=StackActionResponse)
async def run_stack_action(
    request: Request,
    stack_id: int,
    action_name: str,
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
        request.app.state.reconciler.enqueue(stack.name)
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
    tenant_row = await tenancy.get_tenant(db, stack.owner_id)
    if tenant_row is None or tenant_row.uid is None:
        raise ConflictError("Stack tenant is not provisioned yet; no logs available")
    logs = await systemd_user.journal(
        tenant_row.linux_user,
        quadlet.service_unit_name(stack.name, chosen.name),
        uid=tenant_row.uid,
        tail=tail,
    )
    return StackLogsResponse(service=chosen.name, logs=logs)


@router.get("/{stack_id}/connections", response_model=list[ConnectionLinkResponse])
async def stack_connections(
    request: Request,
    stack_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Copy-paste DB connection URIs (internal / host-loopback / public) for a
    stack's database service. Owner-scoped — the URIs embed the credentials."""
    stack = await fetch_owned_stack(db, user, stack_id)
    services, _, _ = await stacks_service.stack_children(db, stack.id)
    links = stacks_service.connection_links(stack, services, _settings(request))
    return [
        ConnectionLinkResponse(
            service=link.service,
            scheme=link.scheme,
            exposed=link.exposed,
            internal_uri=link.internal_uri,
            host_uri=link.host_uri,
            public_uri=link.public_uri,
        )
        for link in links
    ]


@router.put(
    "/{stack_id}/services/{service_name}/expose",
    response_model=StackOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def set_service_exposure(
    request: Request,
    stack_id: int,
    service_name: str,
    body: ExposeServiceRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Toggle public reachability of a service's published port. Exposed →
    the port binds 0.0.0.0 (reachable on the server's public IP); otherwise
    loopback-only. Flips the unit's PublishPort and re-converges."""
    stack = await fetch_owned_stack(db, user, stack_id)
    if stack.status == "deleting":
        raise ConflictError("Stack is being deleted")
    services, _, _ = await stacks_service.stack_children(db, stack.id)
    svc = next((s for s in services if s.name == service_name), None)
    if svc is None:
        raise NotFoundError("Service not found")
    if svc.internal_port is None:
        raise StackValidationError("This service publishes no port to expose")

    svc.publicly_exposed = body.exposed
    stack.generation += 1
    op = Operation(kind="converge_stack", stack_id=stack.id, domain=stack.name)
    db.add(op)
    await db.commit()
    await db.refresh(op)

    request.app.state.reconciler.enqueue(stack.name, op.id)
    return StackOperationAccepted(stack=await stack_response(db, stack, request), operation_id=op.id)


class SetPostStartCommandRequest(BaseModel):
    service_name: str
    command: str = Field(default="", max_length=1024)


@router.put(
    "/{stack_id}/post-start-command",
    response_model=StackOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def set_post_start_command(
    request: Request,
    stack_id: int,
    body: SetPostStartCommandRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Set (or clear) the post-start command for one service. The command runs
    inside the container after it starts — use it for DB migrations, cache
    warming, etc. An empty command disables the feature."""
    stack = await fetch_owned_stack(db, user, stack_id)
    if stack.status == "deleting":
        raise ConflictError("Stack is being deleted")
    services, _, _ = await stacks_service.stack_children(db, stack.id)
    svc = next((s for s in services if s.name == body.service_name), None)
    if svc is None:
        raise NotFoundError(f"Service {body.service_name!r} not found")

    svc.post_start_command = body.command.strip() or None
    stack.generation += 1
    op = Operation(kind="converge_stack", stack_id=stack.id, domain=stack.name)
    db.add(op)
    await db.commit()
    await db.refresh(op)

    request.app.state.reconciler.enqueue(stack.name, op.id)
    return StackOperationAccepted(stack=await stack_response(db, stack, request), operation_id=op.id)


# ==============================================================================
# COOLIFY-STYLE MOCK ENDPOINTS (Tags, Scheduled Tasks, Deployments, Metrics, etc)
# ==============================================================================

class ConfigUpdateRequest(BaseModel):
    repo: str | None = None
    branch: str | None = None
    auto_deploy: bool | None = None
    force_rebuild: bool | None = None
    source_id: int | None = None

@router.patch("/{stack_id}/config")
async def update_stack_config(
    stack_id: int,
    body: ConfigUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    stack = await fetch_owned_stack(db, user, stack_id)
    settings = _settings(request)
    inputs = stacks_service.decrypt_inputs(stack, settings)

    if body.repo is not None:
        inputs["repo"] = body.repo
    if body.branch is not None:
        inputs["branch"] = body.branch
    if body.auto_deploy is not None:
        inputs["auto_deploy"] = body.auto_deploy
    if body.force_rebuild is not None:
        inputs["force_rebuild"] = body.force_rebuild
    if body.source_id is not None:
        if body.source_id == 0:
            inputs.pop("source_id", None)
        else:
            source = await db.get(GitSource, body.source_id)
            if not source or source.owner_id != user.id:
                raise NotFoundError("Git source not found")
            inputs["source_id"] = body.source_id

    stack.inputs_encrypted = stacks_service.encrypt_inputs(inputs, settings)
    await db.commit()
    return {"status": "ok"}


@router.get("/{stack_id}/tags")
async def get_stack_tags(
    stack_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await fetch_owned_stack(db, user, stack_id)
    result = await db.execute(
        select(Tag).join(StackTag).where(StackTag.stack_id == stack_id)
    )
    return [{"id": t.id, "name": t.name} for t in result.scalars()]


class TagCreateRequest(BaseModel):
    name: str


@router.post("/{stack_id}/tags")
async def add_stack_tag(
    stack_id: int,
    body: TagCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await fetch_owned_stack(db, user, stack_id)
    tag = await db.scalar(select(Tag).where(Tag.name == body.name))
    if not tag:
        tag = Tag(name=body.name)
        db.add(tag)
        await db.flush()
    existing = await db.scalar(
        select(StackTag).where(StackTag.stack_id == stack_id, StackTag.tag_id == tag.id)
    )
    if not existing:
        db.add(StackTag(stack_id=stack_id, tag_id=tag.id))
        await db.commit()
    return {"status": "ok", "tag": {"id": tag.id, "name": tag.name}}


@router.delete("/{stack_id}/tags/{tag_id}")
async def remove_stack_tag(
    stack_id: int,
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await fetch_owned_stack(db, user, stack_id)
    st = await db.scalar(
        select(StackTag).where(StackTag.stack_id == stack_id, StackTag.tag_id == tag_id)
    )
    if st:
        await db.delete(st)
        await db.commit()
    return {"status": "ok"}


@router.get("/{stack_id}/scheduled-tasks")
async def get_scheduled_tasks(
    stack_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await fetch_owned_stack(db, user, stack_id)
    result = await db.execute(select(ScheduledTask).where(ScheduledTask.stack_id == stack_id))
    return [
        {"id": t.id, "name": t.name, "command": t.command, "cron_schedule": t.cron_schedule}
        for t in result.scalars()
    ]


class ScheduledTaskCreate(BaseModel):
    name: str = Field(..., max_length=64)
    command: str = Field(..., max_length=512, pattern=r'^[a-zA-Z0-9_./ \-]+$')
    cron_schedule: str = Field(..., max_length=64)
    enabled: bool = True

    @field_validator("cron_schedule")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        parts = v.strip().split()
        if len(parts) != 5:
            raise ValueError("cron_schedule must have exactly 5 fields (minute hour dom month dow)")
        return v.strip()


@router.post("/{stack_id}/scheduled-tasks")
async def add_scheduled_task(
    stack_id: int,
    body: ScheduledTaskCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await fetch_owned_stack(db, user, stack_id)
    task = ScheduledTask(
        stack_id=stack_id, name=body.name, command=body.command, cron_schedule=body.cron_schedule
    )
    db.add(task)
    await db.commit()
    return {"status": "ok"}


@router.delete("/{stack_id}/scheduled-tasks/{task_id}")
async def remove_scheduled_task(
    stack_id: int,
    task_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stack = await fetch_owned_stack(db, user, stack_id)
    task = await db.scalar(
        select(ScheduledTask).where(
            ScheduledTask.id == task_id, ScheduledTask.stack_id == stack.id
        )
    )
    if task:
        await db.delete(task)
        await db.commit()
    return {"status": "ok"}


@router.get("/{stack_id}/operations")
async def list_stack_operations(
    stack_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Recent operations for a stack, newest first."""
    import json as _json
    await fetch_owned_stack(db, user, stack_id)
    rows = (
        await db.execute(
            select(Operation)
            .where(Operation.stack_id == stack_id)
            .order_by(Operation.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": op.id,
            "kind": op.kind,
            "domain": op.domain,
            "status": op.status,
            "steps": _json.loads(op.steps_json),
            "error": op.error,
            "log_lines": op.log_lines or "",
            "created_at": op.created_at.isoformat(),
            "finished_at": op.finished_at.isoformat() if op.finished_at else None,
        }
        for op in rows
    ]


@router.get("/{stack_id}/deployments")
async def get_deployments(
    stack_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await fetch_owned_stack(db, user, stack_id)
    result = await db.execute(
        select(Deployment)
        .where(Deployment.stack_id == stack_id)
        .order_by(Deployment.created_at.desc())
    )
    return [
        {
            "id": d.id,
            "commit_sha": d.commit_sha,
            "status": d.status,
            "message": d.message,
            "created_at": d.created_at.isoformat(),
        }
        for d in result.scalars()
    ]


@router.post("/{stack_id}/deployments/{deployment_id}/rollback")
async def rollback_deployment(
    stack_id: int,
    deployment_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stack = await fetch_owned_stack(db, user, stack_id)
    deployment = await db.scalar(
        select(Deployment).where(
            Deployment.id == deployment_id, Deployment.stack_id == stack.id
        )
    )
    if not deployment:
        raise NotFoundError("Deployment not found")

    stack.generation += 1
    op = Operation(kind="converge_stack", stack_id=stack.id, domain=stack.name)
    db.add(op)
    new_dep = Deployment(
        stack_id=stack_id,
        commit_sha=deployment.commit_sha,
        status="running",
        message=f"Rollback to {deployment.commit_sha}",
    )
    db.add(new_dep)
    await db.commit()
    request.app.state.reconciler.enqueue(stack.name, op.id)
    return {"status": "ok", "operation_id": op.id}


@router.get("/{stack_id}/webhook_info")
async def get_webhook_info(
    stack_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await fetch_owned_stack(db, user, stack_id)
    settings = _settings(request)
    # Derive a stable, server-secret–dependent webhook token. Not guessable
    # without HOSTY_SECRET_KEY even if the stack_id is known.
    token = hmac.new(
        settings.secret_key.encode(), f"webhook:{stack_id}".encode(), hashlib.sha256
    ).hexdigest()
    return {
        "url": f"{request.base_url}api/stacks/webhooks/{stack_id}",
        "secret": f"whsec_{token[:32]}",
    }


@router.get("/{stack_id}/metrics")
async def get_stack_metrics(
    stack_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await fetch_owned_stack(db, user, stack_id)
    return []
