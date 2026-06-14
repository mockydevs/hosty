"""Liveness/readiness endpoint with reconciler diagnostics."""

from __future__ import annotations
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.deps import get_db
from app.db.models import Operation, Stack

router = APIRouter()


async def _migration_status(db: AsyncSession) -> dict:
    """Return current vs expected Alembic heads."""
    try:
        from alembic.config import Config as _Cfg
        from alembic.runtime.migration import MigrationContext as _MCtx
        from alembic.script import ScriptDirectory as _Script

        ini = Path(__file__).parent.parent.parent.parent / "alembic.ini"
        if not ini.exists():
            return {"status": "unknown", "reason": "alembic.ini not found"}
        cfg = _Cfg(str(ini))
        script = _Script.from_config(cfg)
        expected = set(script.get_heads())
        current = set()

        def _get_heads(sync_conn):
            return set(_MCtx.configure(sync_conn).get_current_heads())

        current = await db.run_sync(_get_heads)
        if current == expected:
            return {"status": "ok", "head": list(current)}
        return {
            "status": "behind",
            "current": list(current),
            "expected": list(expected),
        }
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}


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

    migration = await _migration_status(db)

    degraded = any(
        [
            database != "ok",
            migration.get("status") == "behind",
            stack_counts.get("degraded", 0) > 0,
            op_counts.get("running", 0) > 10,
        ]
    )

    return {
        "status": "degraded" if degraded else "ok",
        "version": __version__,
        "database": database,
        "migration": migration,
        "reconciler": {
            "queue_depth": queue_depth,
            "enabled": reconciler is not None,
        },
        "operations": op_counts,
        "stacks": stack_counts,
    }
