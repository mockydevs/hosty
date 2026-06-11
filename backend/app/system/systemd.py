"""systemd unit management via systemctl.

Degrades gracefully when systemd is unavailable (dev containers): `status`
reports `available=False` instead of failing, while `control` raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.errors import AppError
from app.system import runner

UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@\-]{0,63}$")
CONTROL_ACTIONS = frozenset({"start", "stop", "restart", "reload", "enable", "disable"})

_SHOW_PROPERTIES = "--property=LoadState,ActiveState,SubState,UnitFileState"


class SystemdError(AppError):
    status_code = 502
    code = "systemd_error"


class InvalidUnitError(runner.InvalidCommandError):
    pass


@dataclass(frozen=True)
class ServiceStatus:
    unit: str
    available: bool
    active_state: str
    sub_state: str
    enabled: str


def validate_unit(unit: str) -> str:
    # fullmatch, not match: `$` would match before a trailing newline.
    if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
        raise InvalidUnitError(f"Invalid systemd unit name: {unit!r}")
    return unit


def build_status_argv(unit: str) -> list[str]:
    return ["systemctl", "show", validate_unit(unit), _SHOW_PROPERTIES, "--no-pager"]


def build_control_argv(action: str, unit: str) -> list[str]:
    if action not in CONTROL_ACTIONS:
        raise InvalidUnitError(f"Invalid systemd action: {action!r}")
    return ["systemctl", action, validate_unit(unit)]


def parse_show_output(unit: str, output: str) -> ServiceStatus:
    props: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return ServiceStatus(
        unit=unit,
        available=props.get("LoadState", "not-found") == "loaded",
        active_state=props.get("ActiveState", "unknown"),
        sub_state=props.get("SubState", "unknown"),
        enabled=props.get("UnitFileState", "unknown"),
    )


def _unavailable(unit: str) -> ServiceStatus:
    return ServiceStatus(
        unit=unit, available=False, active_state="unknown", sub_state="unknown", enabled="unknown"
    )


async def status(unit: str) -> ServiceStatus:
    try:
        result = await runner.run(build_status_argv(unit), timeout=10)
    except runner.CommandNotFoundError:
        return _unavailable(unit)  # systemctl not installed (dev container)
    if not result.ok:
        return _unavailable(unit)  # systemd not running on this host
    return parse_show_output(unit, result.stdout)


async def control(action: str, unit: str) -> ServiceStatus:
    argv = build_control_argv(action, unit)
    try:
        result = await runner.run(argv, timeout=60)
    except runner.CommandNotFoundError as exc:
        raise SystemdError("systemd is not available on this host") from exc
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise SystemdError(f"systemctl {action} {unit} failed: {detail}")
    return await status(unit)
