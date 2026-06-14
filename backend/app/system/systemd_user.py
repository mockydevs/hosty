"""Tenant user-manager control via `systemctl --machine <user>@.host --user`
(v2/M2, ADR-013). Root drives each tenant's lingering systemd instance
through machined; nothing here ever runs as the tenant via su/sudo.

Grammar: tenant usernames through `tenants.validate_tenant_username`
(structurally only `hosty-t-<digits>`), unit names through the same
allowlist as system/systemd.py. Graceful-degradation contract identical to
systemd.py: `status` reports unavailable instead of failing; `control`
raises.
"""

from __future__ import annotations

from app.core.errors import AppError
from app.system import runner
from app.system.systemd import (
    _SHOW_PROPERTIES,
    ServiceStatus,
    _unavailable,
    parse_show_output,
    validate_unit,
)
from app.system.tenants import validate_tenant_username

CONTROL_ACTIONS = frozenset({"start", "stop", "restart"})


class SystemdUserError(AppError):
    status_code = 502
    code = "systemd_user_error"


class InvalidSystemdUserArgError(runner.InvalidCommandError):
    pass


def _machine(user: str) -> str:
    return f"{validate_tenant_username(user)}@.host"


# --- argv builders (pure) ----------------------------------------------------------


def build_daemon_reload_argv(user: str) -> list[str]:
    return ["systemctl", "--machine", _machine(user), "--user", "daemon-reload"]


def build_control_argv(user: str, action: str, unit: str) -> list[str]:
    if action not in CONTROL_ACTIONS:
        raise InvalidSystemdUserArgError(f"Invalid systemd user action: {action!r}")
    return ["systemctl", "--machine", _machine(user), "--user", action, validate_unit(unit)]


def build_show_argv(user: str, unit: str) -> list[str]:
    return [
        "systemctl",
        "--machine",
        _machine(user),
        "--user",
        "show",
        validate_unit(unit),
        _SHOW_PROPERTIES,
        "--no-pager",
    ]


def build_is_active_argv(user: str, unit: str) -> list[str]:
    return [
        "systemctl",
        "--machine",
        _machine(user),
        "--user",
        "is-active",
        "--quiet",
        validate_unit(unit),
    ]


def build_journal_argv(user: str, unit: str, *, uid: int, tail: int = 200) -> list[str]:
    """Read a tenant user-unit's logs from ROOT's merged journal by field
    match. `journalctl -M <user>@.host` does NOT work for a lingering user
    manager — machined only knows registered containers, so it fails with
    "No machine '<user>@.host' known". Matching `_UID` + `_SYSTEMD_USER_UNIT`
    is machined-free, sees crashed/`--rm`'d containers (journald LogDriver
    persists), AND captures the unit's own start-failure messages (e.g. an
    image-pull error) when `podman run` never produced a container."""
    tail = max(1, min(int(tail), 5000))
    validate_tenant_username(user)
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise InvalidSystemdUserArgError(f"Invalid uid: {uid!r}")
    return [
        "journalctl",
        f"_UID={uid}",
        f"_SYSTEMD_USER_UNIT={validate_unit(unit)}",
        "-n",
        str(tail),
        "--no-pager",
        "--output",
        "short-iso",
    ]


# --- async operations --------------------------------------------------------------


async def _run_or_raise(argv: list[str], what: str, *, timeout: float = 60) -> runner.CommandResult:
    try:
        result = await runner.run(argv, timeout=timeout)
    except runner.CommandNotFoundError as exc:
        raise SystemdUserError("systemd is not available on this host") from exc
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise SystemdUserError(f"{what} failed: {detail[:400]}")
    return result


async def daemon_reload(user: str) -> None:
    await _run_or_raise(build_daemon_reload_argv(user), f"daemon-reload for {user}")


async def control(user: str, action: str, unit: str, *, timeout: float | None = None) -> None:
    # start must wait for the full dep chain (git-sync → podman build → container);
    # builds can easily exceed 60s, so default to 10 min for start, 60s for the rest.
    _timeout = timeout if timeout is not None else (600.0 if action == "start" else 60.0)
    await _run_or_raise(build_control_argv(user, action, unit), f"{action} {unit} for {user}", timeout=_timeout)


async def status(user: str, unit: str) -> ServiceStatus:
    """Graceful: unavailable (never an exception) when systemd/machined is
    missing or the tenant's user manager is not running."""
    try:
        result = await runner.run(build_show_argv(user, unit), timeout=10)
    except runner.CommandNotFoundError:
        return _unavailable(unit)
    if not result.ok:
        return _unavailable(unit)
    return parse_show_output(unit, result.stdout)


async def is_active(user: str, unit: str) -> bool:
    try:
        result = await runner.run(build_is_active_argv(user, unit), timeout=10)
    except runner.CommandNotFoundError:
        return False
    return result.ok


async def journal(user: str, unit: str, *, uid: int, tail: int = 200) -> str:
    result = await _run_or_raise(
        build_journal_argv(user, unit, uid=uid, tail=tail), f"journal read for {unit}", timeout=30
    )
    return result.stdout
