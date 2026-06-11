"""Linux site-user management.

Safety invariant: site users ALWAYS match `site-*`. Validation rejects anything
else, so the panel is structurally incapable of touching system accounts
(root, www-data, ...), no matter what input reaches this layer.
"""

from __future__ import annotations

import re

from app.system import runner

SITE_USER_RE = re.compile(r"^site-[a-z0-9][a-z0-9-]{1,24}$")


class InvalidSiteUserError(runner.InvalidCommandError):
    pass


class UserOperationError(RuntimeError):
    pass


def validate_site_username(name: str) -> str:
    # fullmatch, not match: `$` would match before a trailing newline.
    if not isinstance(name, str) or not SITE_USER_RE.fullmatch(name):
        raise InvalidSiteUserError(
            f"Invalid site username: {name!r} "
            "(must be 'site-' + 2-25 lowercase alphanumerics/hyphens)"
        )
    return name


def build_useradd_argv(name: str) -> list[str]:
    name = validate_site_username(name)
    return [
        "useradd",
        "--create-home",
        "--home-dir",
        f"/home/{name}",
        "--shell",
        "/usr/sbin/nologin",
        "--comment",
        "hosty site user",
        name,
    ]


def build_userdel_argv(name: str) -> list[str]:
    return ["userdel", "--remove", validate_site_username(name)]


async def exists(name: str) -> bool:
    validate_site_username(name)
    try:
        result = await runner.run(["id", "-u", name], timeout=10)
    except runner.CommandNotFoundError:
        return False
    return result.ok


async def create(name: str) -> bool:
    """Create a site user. Idempotent: returns False if it already existed."""
    validate_site_username(name)
    if await exists(name):
        return False
    result = await runner.run(build_useradd_argv(name), timeout=30)
    if not result.ok:
        raise UserOperationError(f"useradd failed for {name}: {result.stderr.strip()}")
    return True


async def delete(name: str) -> bool:
    """Delete a site user. Idempotent: returns False if it did not exist."""
    validate_site_username(name)
    if not await exists(name):
        return False
    result = await runner.run(build_userdel_argv(name), timeout=30)
    if not result.ok:
        raise UserOperationError(f"userdel failed for {name}: {result.stderr.strip()}")
    return True
