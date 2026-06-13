"""Loopback host-port allocator (v2/M2, ADR-013). One ledger for every
workload system that publishes on 127.0.0.1.

The DB is the ledger and the UNIQUE constraint is the lock: allocation
returns the lowest free port, and a concurrent race loses at COMMIT (an
IntegrityError the caller retries), never at runtime on the host. Pattern
proven by the 12a apps allocator, which now delegates here.

Until M6 removes the Apps MVP, used ports are the union of `apps.host_port`
and `stack_services.host_port` (the latter joins with migration 0015/M4 —
`_used_port_columns` is the single place to extend).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import App, StackService


class NoFreePortError(AppError):
    status_code = 409
    code = "no_free_port"


def _used_port_columns():
    """Every column that holds an allocated loopback port. M4 adds
    StackService.host_port; M6 drops App.host_port."""
    return [App.host_port, StackService.host_port]


async def used_host_ports(db: AsyncSession) -> set[int]:
    used: set[int] = set()
    for column in _used_port_columns():
        used.update(
            port for port in (await db.execute(select(column))).scalars().all() if port is not None
        )
    return used


async def allocate_host_port(db: AsyncSession, settings: Settings) -> int:
    """Lowest free port in [app_port_min, app_port_max]. Callers commit the
    row holding the port; on IntegrityError (lost race) they retry."""
    used = await used_host_ports(db)
    for port in range(settings.app_port_min, settings.app_port_max + 1):
        if port not in used:
            return port
    raise NoFreePortError("No free loopback ports left on this server")
