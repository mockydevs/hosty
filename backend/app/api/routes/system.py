"""System service management. Units are restricted to a managed allowlist."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_admin
from app.core.errors import AppError, ConflictError, NotFoundError
from app.services import panel_config, stats
from app.services import sites as sites_service
from app.services.sites import DomainValidationError, validate_domain
from app.system import systemd

# Reads (stats, service status) are visible to every authenticated user;
# mutations (service control, panel domain) are admin-only (Phase 11a).
router = APIRouter(dependencies=[Depends(get_current_user)])
_admin = Depends(require_admin)


class PanelDomainError(AppError):
    status_code = 502
    code = "panel_domain_failed"


class SystemStatsResponse(BaseModel):
    model_config = {"from_attributes": True}

    cpu_percent: float
    load_avg: list[float]
    memory_total: int
    memory_used: int
    memory_percent: float
    disk_total: int
    disk_used: int
    disk_percent: float
    uptime_seconds: int


@router.get("/stats", response_model=SystemStatsResponse)
async def system_stats() -> stats.SystemStats:
    return stats.collect()


class ServiceStatusResponse(BaseModel):
    model_config = {"from_attributes": True}

    unit: str
    available: bool
    active_state: str
    sub_state: str
    enabled: str


def _check_unit(request: Request, unit: str) -> None:
    if unit not in request.app.state.settings.managed_units:
        raise NotFoundError(f"Unknown service: {unit}")


@router.get("/services", response_model=list[ServiceStatusResponse])
async def list_services(request: Request) -> list[systemd.ServiceStatus]:
    return [await systemd.status(unit) for unit in request.app.state.settings.managed_units]


@router.get("/services/{unit}", response_model=ServiceStatusResponse)
async def service_status(request: Request, unit: str) -> systemd.ServiceStatus:
    _check_unit(request, unit)
    return await systemd.status(unit)


@router.post(
    "/services/{unit}/actions/{action}",
    response_model=ServiceStatusResponse,
    dependencies=[_admin],
)
async def service_action(request: Request, unit: str, action: str) -> systemd.ServiceStatus:
    _check_unit(request, unit)
    if action not in systemd.CONTROL_ACTIONS:
        raise NotFoundError(f"Unknown action: {action}")
    return await systemd.control(action, unit)


# --- panel domain / HTTPS -------------------------------------------------------------


class PanelDomainResponse(BaseModel):
    domain: str | None
    cookie_secure: bool
    url: str | None  # https URL once a domain is configured


class SetPanelDomainRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    # Skip the DNS-points-here check (e.g. DNS is still propagating).
    force: bool = False


def _panel_response(settings: Any) -> PanelDomainResponse:
    domain = settings.panel_domain or None
    return PanelDomainResponse(
        domain=domain,
        cookie_secure=settings.cookie_secure,
        url=f"https://{domain}" if domain else None,
    )


@router.get("/panel-domain", response_model=PanelDomainResponse)
async def get_panel_domain(request: Request) -> PanelDomainResponse:
    return _panel_response(request.app.state.settings)


@router.put("/panel-domain", response_model=PanelDomainResponse, dependencies=[_admin])
async def set_panel_domain(
    request: Request, body: SetPanelDomainRequest, db: AsyncSession = Depends(get_db)
) -> PanelDomainResponse:
    """Serve the panel on a domain over HTTPS.

    Publishes the vhost to Caddy (which obtains the certificate), persists
    HOSTY_PANEL_DOMAIN + HOSTY_COOKIE_SECURE to the env file, and enables
    secure cookies immediately. The current plain-HTTP session keeps working
    until its access token expires; log in again on the HTTPS URL.
    """
    settings = request.app.state.settings
    try:
        domain = validate_domain(body.domain)
    except DomainValidationError as exc:
        raise ConflictError(str(exc)) from exc

    if not body.force:
        ips = await panel_config.resolve_ips(domain)
        if not ips:
            raise ConflictError(
                f"{domain} does not resolve yet. Point an A record at this server "
                "first, or retry with the override once DNS is set."
            )
        if settings.public_ip and settings.public_ip not in ips:
            raise ConflictError(
                f"{domain} resolves to {', '.join(ips)}, not to this server "
                f"({settings.public_ip}). Fix the A record or retry with the override."
            )

    old_domain, old_secure = settings.panel_domain, settings.cookie_secure
    settings.panel_domain = domain
    settings.cookie_secure = True
    try:
        await sites_service.resync_caddy(db, settings)
    except Exception as exc:
        settings.panel_domain = old_domain
        settings.cookie_secure = old_secure
        raise PanelDomainError(f"Could not publish the panel vhost: {exc}") from exc

    panel_config.update_env_file(
        settings.env_file_path,
        {"HOSTY_PANEL_DOMAIN": domain, "HOSTY_COOKIE_SECURE": "true"},
    )
    return _panel_response(settings)


@router.delete("/panel-domain", response_model=PanelDomainResponse, dependencies=[_admin])
async def clear_panel_domain(request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    """Back to IP-only access: remove the vhost and allow cookies over HTTP."""
    settings = request.app.state.settings
    if not settings.panel_domain:
        raise NotFoundError("No panel domain configured")

    old_domain, old_secure = settings.panel_domain, settings.cookie_secure
    settings.panel_domain = None
    settings.cookie_secure = False
    try:
        await sites_service.resync_caddy(db, settings)
    except Exception as exc:
        settings.panel_domain = old_domain
        settings.cookie_secure = old_secure
        raise PanelDomainError(f"Could not update the web server: {exc}") from exc

    panel_config.update_env_file(
        settings.env_file_path,
        {"HOSTY_PANEL_DOMAIN": None, "HOSTY_COOKIE_SECURE": "false"},
    )
    return _panel_response(settings)
