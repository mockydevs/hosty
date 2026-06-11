"""System service management (admin-only). Units are restricted to a managed allowlist."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.core.errors import NotFoundError
from app.system import systemd

router = APIRouter(dependencies=[Depends(get_current_user)])


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


@router.post("/services/{unit}/actions/{action}", response_model=ServiceStatusResponse)
async def service_action(request: Request, unit: str, action: str) -> systemd.ServiceStatus:
    _check_unit(request, unit)
    if action not in systemd.CONTROL_ACTIONS:
        raise NotFoundError(f"Unknown action: {action}")
    return await systemd.control(action, unit)
