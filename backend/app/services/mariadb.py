"""MariaDB provisioning (minimal slice for WordPress; Phase 5 extends this).

There is no user-supplied input in any statement: database/user names are
derived from the validated `site-*` user (strict identifier regex enforced
again here), and passwords are generated from a hex alphabet. SQL builders are
pure and unit-tested, including injection attempts. Execution goes through the
runner via the `mariadb` client using the root unix socket.

Phase 5 ADR candidate: replace the CLI with a driver + parameterized SQL once
databases take arbitrary (user-chosen) names.
"""

from __future__ import annotations

import re
import secrets

import structlog

from app.system import runner

log = structlog.get_logger("hosty.mariadb")

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PASSWORD_RE = re.compile(r"^[a-f0-9]{16,64}$")  # token_hex output only


class InvalidIdentifierError(ValueError):
    pass


class MariaDBError(RuntimeError):
    pass


def validate_identifier(name: str) -> str:
    if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name):
        raise InvalidIdentifierError(
            f"Invalid MariaDB identifier: {name!r} (lowercase, [a-z0-9_], 2-64 chars)"
        )
    return name


def _validate_password(password: str) -> str:
    if not isinstance(password, str) or not PASSWORD_RE.fullmatch(password):
        raise InvalidIdentifierError("Password must be a generated hex token")
    return password


def generate_password() -> str:
    return secrets.token_hex(24)


def db_identifiers_for(site_user: str) -> tuple[str, str]:
    """(database, user) derived from the site user: site-a-1b2c3d → wp_a_1b2c3d."""
    slug = site_user.removeprefix("site-").replace("-", "_")
    name = validate_identifier(f"wp_{slug}"[:64])
    return name, name


def build_create_sql(database: str, user: str, password: str) -> str:
    """Pure. Least privilege: rights on the one database only, localhost only."""
    database = validate_identifier(database)
    user = validate_identifier(user)
    password = _validate_password(password)
    return (
        f"CREATE DATABASE IF NOT EXISTS `{database}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
        f"CREATE USER IF NOT EXISTS '{user}'@'localhost' IDENTIFIED BY '{password}'; "
        f"ALTER USER '{user}'@'localhost' IDENTIFIED BY '{password}'; "
        f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{user}'@'localhost'; "
        "FLUSH PRIVILEGES;"
    )


def build_drop_sql(database: str, user: str) -> str:
    """Pure. Idempotent: IF EXISTS on both."""
    database = validate_identifier(database)
    user = validate_identifier(user)
    return (
        f"DROP DATABASE IF EXISTS `{database}`; "
        f"DROP USER IF EXISTS '{user}'@'localhost'; "
        "FLUSH PRIVILEGES;"
    )


def build_mariadb_argv(sql: str) -> list[str]:
    return ["mariadb", "--protocol=socket", "--user=root", "--batch", "--execute", sql]


async def _execute(sql: str, what: str) -> None:
    result = await runner.run(build_mariadb_argv(sql), timeout=30)
    if not result.ok:
        raise MariaDBError(f"{what} failed: {result.stderr.strip()[:300]}")


async def create_database(database: str, user: str, password: str) -> None:
    await _execute(build_create_sql(database, user, password), f"create database {database}")
    log.info("mariadb_database_created", database=database, user=user)


async def drop_database(database: str, user: str) -> None:
    await _execute(build_drop_sql(database, user), f"drop database {database}")
    log.info("mariadb_database_dropped", database=database, user=user)


def build_drop_database_only_sql(database: str) -> str:
    """Pure. Drops just the schema — for orphans, where no panel user exists."""
    database = validate_identifier(database)
    return f"DROP DATABASE IF EXISTS `{database}`;"


async def drop_orphan_database(database: str) -> None:
    await _execute(build_drop_database_only_sql(database), f"drop orphan database {database}")
    log.info("mariadb_orphan_database_dropped", database=database)


# --- Phase 5: user-managed databases, reset password, orphan detection -----------

SYSTEM_SCHEMAS = frozenset({"information_schema", "performance_schema", "mysql", "sys"})


def build_reset_password_sql(user: str, password: str) -> str:
    """Pure. ALTER USER for an existing panel-managed user."""
    user = validate_identifier(user)
    password = _validate_password(password)
    return f"ALTER USER '{user}'@'localhost' IDENTIFIED BY '{password}'; FLUSH PRIVILEGES;"


async def reset_password(user: str, password: str) -> None:
    await _execute(build_reset_password_sql(user, password), f"reset password for {user}")
    log.info("mariadb_password_reset", user=user)


def build_show_databases_argv() -> list[str]:
    return [
        "mariadb",
        "--protocol=socket",
        "--user=root",
        "--batch",
        "--skip-column-names",
        "--execute",
        "SHOW DATABASES;",
    ]


async def list_physical_databases() -> list[str]:
    """All non-system schemas that exist on the server (for orphan detection)."""
    result = await runner.run(build_show_databases_argv(), timeout=30)
    if not result.ok:
        raise MariaDBError(f"SHOW DATABASES failed: {result.stderr.strip()[:300]}")
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and line.strip() not in SYSTEM_SCHEMAS
    ]
