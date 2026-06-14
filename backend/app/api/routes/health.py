"""Liveness/readiness endpoint with reconciler diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.deps import get_db
from app.db.models import Operation, Stack

router = APIRouter()


@router.get("/health")
async def health(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        await db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "error"

    # Reconciler queue depth (in-memory; 0 when not running in test mode)
    reconciler = getattr(request.app.state, "reconciler", None)
    queue_depth = reconciler._queue.qsize() if reconciler is not None else 0

    # Count ops by status
    op_counts: dict[str, int] = {}
    try:
        rows = (
            await db.execute(
                select(Operation.status, func.count().label("n"))
                .group_by(Operation.status)
            )
        ).all()
        op_counts = {r.status: r.n for r in rows}
    except Exception:
        pass

    # Stack status summary
    stack_counts: dict[str, int] = {}
    try:
        rows = (
            await db.execute(
                select(Stack.status, func.count().label("n")).group_by(Stack.status)
            )
        ).all()
        stack_counts = {r.status: r.n for r in rows}
    except Exception:
        pass

    degraded = any(
        [
            database != "ok",
            stack_counts.get("degraded", 0) > 0,
            op_counts.get("running", 0) > 10,
        ]
    )

    return {
        "status": "degraded" if degraded else "ok",
        "version": __version__,
        "database": database,
        "reconciler": {
            "queue_depth": queue_depth,
            "enabled": reconciler is not None,
        },
        "operations": op_counts,
        "stacks": stack_counts,
    }
