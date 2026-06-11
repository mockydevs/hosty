"""Audit log API (Week 22): paged view of all recorded mutating actions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.db.models import AuditLog

router = APIRouter(dependencies=[Depends(get_current_user)])


class AuditEntryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    user_id: int | None
    username: str | None
    method: str
    path: str
    status_code: int
    client_ip: str | None
    created_at: datetime


class AuditPageResponse(BaseModel):
    total: int
    entries: list[AuditEntryResponse]


@router.get("", response_model=AuditPageResponse)
async def list_audit_entries(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Any:
    total = (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    rows = (
        (
            await db.execute(
                select(AuditLog).order_by(AuditLog.id.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return AuditPageResponse(
        total=total,
        entries=[AuditEntryResponse.model_validate(r) for r in rows],
    )
