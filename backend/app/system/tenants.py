"""Tenant Linux-user management (v2/M0, ADR-013).

One client account = one Linux user = one kernel-enforced isolation boundary.
Tenant containers run rootless under this uid; quotas land on its systemd
slice; volumes live in its home.

Safety invariant: tenant users ALWAYS match `hosty-t-<digits>`. Validation
rejects anything else, so this layer is structurally incapable of touching
system accounts or site-* users, no matter what input reaches it.

Subuid/subgid ranges are allocated by the panel's ledger (the `tenants`
table, services/tenancy.py) and applied here — never auto-assigned by
useradd, so the panel always knows exactly which host uids belong to whom.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.system import runner

TENANT_USER_RE = re.compile(r"^hosty-t-[0-9]{1,10}$")

# Each tenant gets a full 16-bit uid range (enough for any image's uids).
SUBID_COUNT = 65536
# First allocated range starts well above typical /etc/subuid entries
# (ubuntu's default user gets 100000-165535) to avoid any overlap.
SUBID_BASE = 1_000_000


class InvalidTenantUserError(runner.InvalidCommandError):
    pass


class TenantOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class TenantInfo:
    linux_user: str
    uid: int
    subuid_start: int
    subuid_count: int


def linux_user_for(user_id: int) -> str:
    return validate_tenant_username(f"hosty-t-{int(user_id)}")


def home_dir_for(linux_user: str) -> str:
    return f"/home/{validate_tenant_username(linux_user)}"


def subid_range_for(index: int) -> tuple[int, int]:
    """Ledger policy: the n-th tenant ever created gets a fixed, disjoint
    range. Pure — the DB stores the result so reuse after deletion is a
    deliberate ledger decision, never an accident."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise InvalidTenantUserError(f"Invalid tenant index: {index!r}")
    start = SUBID_BASE + index * SUBID_COUNT
    return start, SUBID_COUNT


def validate_tenant_username(name: str) -> str:
    # fullmatch, not match: `$` would match before a trailing newline.
    if not isinstance(name, str) or not TENANT_USER_RE.fullmatch(name):
        raise InvalidTenantUserError(f"Invalid tenant username: {name!r} (must be 'hosty-t-<id>')")
    return name


def _validate_range(start: int, count: int) -> tuple[int, int]:
    ok = (
        isinstance(start, int)
        and isinstance(count, int)
        and not isinstance(start, bool)
        and not isinstance(count, bool)
        and start >= SUBID_BASE
        and count > 0
    )
    if not ok:
        raise InvalidTenantUserError(f"Invalid subid range: {start!r}+{count!r}")
    return start, count


# --- argv builders (pure) ----------------------------------------------------------


def build_useradd_argv(name: str) -> list[str]:
    name = validate_tenant_username(name)
    return [
        "useradd",
        "--create-home",
        "--home-dir",
        home_dir_for(name),
        "--shell",
        "/usr/sbin/nologin",
        "--comment",
        "hosty tenant",
        name,
    ]


def build_userdel_argv(name: str) -> list[str]:
    return ["userdel", "--remove", validate_tenant_username(name)]


def build_add_subuids_argv(name: str, start: int, count: int) -> list[str]:
    name = validate_tenant_username(name)
    start, count = _validate_range(start, count)
    return ["usermod", "--add-subuids", f"{start}-{start + count - 1}", name]


def build_add_subgids_argv(name: str, start: int, count: int) -> list[str]:
    name = validate_tenant_username(name)
    start, count = _validate_range(start, count)
    return ["usermod", "--add-subgids", f"{start}-{start + count - 1}", name]


def build_linger_argv(name: str, *, enable: bool = True) -> list[str]:
    action = "enable-linger" if enable else "disable-linger"
    return ["loginctl", action, validate_tenant_username(name)]


def build_uid_argv(name: str) -> list[str]:
    return ["id", "-u", validate_tenant_username(name)]


# --- async host operations ---------------------------------------------------------


async def _run_or_raise(argv: list[str], what: str, *, timeout: float = 30) -> runner.CommandResult:
    result = await runner.run(argv, timeout=timeout)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise TenantOperationError(f"{what} failed: {detail[:400]}")
    return result


async def exists(name: str) -> bool:
    validate_tenant_username(name)
    try:
        result = await runner.run(build_uid_argv(name), timeout=10)
    except runner.CommandNotFoundError:
        return False
    return result.ok


async def uid_of(name: str) -> int:
    result = await _run_or_raise(build_uid_argv(name), f"uid lookup for {name}", timeout=10)
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise TenantOperationError(f"Unparseable uid for {name}: {result.stdout!r}") from exc


async def provision(name: str, *, subuid_start: int, subuid_count: int) -> TenantInfo:
    """Create the tenant user with its subid ranges and lingering user
    manager. Idempotent: an existing user only has the (idempotent)
    subid/linger steps re-applied."""
    validate_tenant_username(name)
    if not await exists(name):
        await _run_or_raise(build_useradd_argv(name), f"useradd {name}")
    # usermod --add-subuids is additive and tolerates re-runs of the same range.
    await _run_or_raise(build_add_subuids_argv(name, subuid_start, subuid_count), "add-subuids")
    await _run_or_raise(build_add_subgids_argv(name, subuid_start, subuid_count), "add-subgids")
    # Lingering keeps the tenant's systemd user instance (and thus their
    # containers) running without a login session, across reboots.
    await _run_or_raise(build_linger_argv(name, enable=True), "enable-linger")
    return TenantInfo(
        linux_user=name,
        uid=await uid_of(name),
        subuid_start=subuid_start,
        subuid_count=subuid_count,
    )


async def remove(name: str) -> bool:
    """Delete the tenant user and home. Idempotent: False if absent.
    Callers must have torn down the tenant's units first (M2's RemoveUnits);
    linger is disabled before deletion so the user manager exits."""
    validate_tenant_username(name)
    if not await exists(name):
        return False
    await _run_or_raise(build_linger_argv(name, enable=False), "disable-linger")
    await _run_or_raise(build_userdel_argv(name), f"userdel {name}", timeout=60)
    return True
