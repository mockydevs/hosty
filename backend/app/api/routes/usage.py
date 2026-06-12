"""Usage views (Phase 11c/11d).

Clients see their own sites' disk/DB/bandwidth usage; admins see a per-client
summary across the server plus a CSV export (billing groundwork). Whole-server
stats stay on /api/system (admin-only).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_admin
from app.core.errors import AppError
from app.db.models import Site, User
from app.services import quotas, usage

router = APIRouter()

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class InvalidMonthError(AppError):
    status_code = 422
    code = "invalid_month"


def _validate_month(month: str | None) -> str | None:
    if month is not None and not MONTH_RE.fullmatch(month):
        raise InvalidMonthError("month must look like 2026-06")
    return month


class SiteUsageResponse(BaseModel):
    site_id: int
    domain: str
    disk_bytes: int
    db_bytes: int
    bandwidth_bytes: int


class MyUsageResponse(BaseModel):
    sites: list[SiteUsageResponse]
    disk_bytes: int
    db_bytes: int
    bandwidth_bytes: int
    max_disk_mb: int | None
    max_sites: int | None
    max_databases: int | None


class ClientUsageResponse(BaseModel):
    user_id: int
    username: str
    site_count: int
    disk_bytes: int
    db_bytes: int
    bandwidth_bytes: int
    max_disk_mb: int | None


@router.get("/me", response_model=MyUsageResponse)
async def my_usage(
    request: Request,
    month: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MyUsageResponse:
    _validate_month(month)
    settings = request.app.state.settings
    sites = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalars().all()
    per_site = await usage.sites_usage(db, settings, list(sites), month=month)
    limits = await quotas.effective_limits(db, user)
    return MyUsageResponse(
        sites=[SiteUsageResponse(**vars(s)) for s in per_site],
        disk_bytes=sum(s.disk_bytes for s in per_site),
        db_bytes=sum(s.db_bytes for s in per_site),
        bandwidth_bytes=sum(s.bandwidth_bytes for s in per_site),
        max_disk_mb=limits.max_disk_mb,
        max_sites=limits.max_sites,
        max_databases=limits.max_databases,
    )


@router.get("", response_model=list[ClientUsageResponse])
async def all_usage(
    request: Request,
    month: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> Any:
    _validate_month(month)
    rows = await usage.all_clients_usage(db, request.app.state.settings, month=month)
    return [
        ClientUsageResponse(
            user_id=r.user_id,
            username=r.username,
            site_count=r.site_count,
            disk_bytes=r.disk_bytes,
            db_bytes=r.db_bytes,
            bandwidth_bytes=r.bandwidth_bytes,
            max_disk_mb=r.max_disk_mb,
        )
        for r in rows
    ]


@router.get("/export", response_class=PlainTextResponse)
async def export_usage(
    request: Request,
    month: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> PlainTextResponse:
    """Exportable monthly summary as CSV (billing groundwork)."""
    _validate_month(month)
    rows = await usage.all_clients_usage(db, request.app.state.settings, month=month)
    return PlainTextResponse(
        usage.usage_csv(rows, month=month),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="hosty-usage-{month or "all"}.csv"'},
    )
