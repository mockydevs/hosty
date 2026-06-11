"""Liveness/readiness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.deps import get_db

router = APIRouter()


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    try:
        await db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "error"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "version": __version__,
        "database": database,
    }
