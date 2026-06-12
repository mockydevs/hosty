"""Operation step recording for planner actions (v2/M3, ADR-013).

Same UI contract as the sites pipeline (`steps_json`: list of
{name, label, status}), extracted so the reconciler can record the
planner's emitted actions as steps WITHOUT touching services/sites.py.
Steps within one stack's plan are unique by construction: action kinds
repeat only across distinct services/volumes, which the name includes.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.db.models import Operation
from app.domain import actions as act


def step_for(action: act.Action) -> tuple[str, str]:
    """(name, label) shown in the operations UI for one planner action."""
    match action:
        case act.EnsureTenant():
            return "tenant", "Provision tenant user"
        case act.EnsureVolumeDir(volume=volume):
            return f"volume:{volume}", f"Create volume {volume}"
        case act.WriteUnits():
            return "units", "Write unit files"
        case act.RemoveUnits():
            return "remove-units", "Remove unit files"
        case act.DaemonReload():
            return "daemon-reload", "Reload tenant service manager"
        case act.StartService(service=service):
            return f"start:{service}", f"Start service {service}"
        case act.StopService(service=service):
            return f"stop:{service}", f"Stop service {service}"
        case act.RestartService(service=service):
            return f"restart:{service}", f"Restart service {service}"
        case act.RemoveVolumeDir(volume=volume):
            return f"remove-volume:{volume}", f"Remove volume {volume}"
        case act.RemoveTenantIfEmpty():
            return "tenant-gc", "Release tenant user if unused"
        case act.SyncCaddy():
            return "ingress", "Sync ingress routes"
    raise ValueError(f"Unknown action: {action!r}")  # pragma: no cover


def initial_steps_json(actions: list[act.Action]) -> str:
    return json.dumps(
        [
            {"name": name, "label": label, "status": "pending"}
            for name, label in (step_for(action) for action in actions)
        ]
    )


async def set_step(db: AsyncSession, op: Operation, name: str, status: str) -> None:
    steps = json.loads(op.steps_json)
    for step in steps:
        if step["name"] == name:
            step["status"] = status
    op.steps_json = json.dumps(steps)
    await db.commit()


async def finish(db: AsyncSession, op: Operation, *, status: str, error: str | None = None) -> None:
    op.status = status
    op.error = error
    op.finished_at = utcnow()
    await db.commit()
