"""MariaDB service: identifier validation, exact SQL builders, injection attempts."""

from __future__ import annotations

import pytest

from app.services.mariadb import (
    InvalidIdentifierError,
    build_create_sql,
    build_drop_sql,
    build_mariadb_argv,
    build_restricted_user_sql,
    db_identifiers_for,
    generate_password,
    validate_identifier,
)


@pytest.mark.parametrize("name", ["wp_example_a1b2c3", "db_1", "a" * 64 if False else "ab"])
def test_valid_identifiers(name):
    assert validate_identifier(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "1abc",  # must start with a letter
        "WP_UPPER",  # uppercase
        "name-with-dash",
        "name with space",
        "name;DROP TABLE users",  # injection
        "name`",  # backtick breakout
        "name'",  # quote breakout
        "a",  # too short
        "a" * 65,  # too long
    ],
)
def test_invalid_identifiers(name):
    with pytest.raises(InvalidIdentifierError):
        validate_identifier(name)


def test_db_identifiers_for_site_user():
    db, user = db_identifiers_for("site-example-com-a1b2c3")
    assert db == "wp_example_com_a1b2c3"
    assert user == db
    assert validate_identifier(db)


def test_generated_password_is_hex_and_long():
    pw = generate_password()
    assert len(pw) == 48
    int(pw, 16)  # raises if not hex


def test_create_sql_snapshot():
    sql = build_create_sql("wp_a_1b2c3d", "wp_a_1b2c3d", "a" * 16)
    assert sql == (
        "CREATE DATABASE IF NOT EXISTS `wp_a_1b2c3d` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
        "CREATE USER IF NOT EXISTS 'wp_a_1b2c3d'@'localhost' IDENTIFIED BY "
        f"'{'a' * 16}'; "
        f"ALTER USER 'wp_a_1b2c3d'@'localhost' IDENTIFIED BY '{'a' * 16}'; "
        "GRANT ALL PRIVILEGES ON `wp_a_1b2c3d`.* TO 'wp_a_1b2c3d'@'localhost'; "
        "FLUSH PRIVILEGES;"
    )


def test_drop_sql_snapshot():
    assert build_drop_sql("wp_a_1b2c3d", "wp_a_1b2c3d") == (
        "DROP DATABASE IF EXISTS `wp_a_1b2c3d`; "
        "DROP USER IF EXISTS 'wp_a_1b2c3d'@'localhost'; "
        "FLUSH PRIVILEGES;"
    )


def test_create_sql_rejects_unsafe_password():
    for bad in ["short", "with'quote" + "a" * 16, "ABCDEF" + "0" * 16, "  " + "a" * 16]:
        with pytest.raises(InvalidIdentifierError):
            build_create_sql("wp_ok", "wp_ok", bad)


def test_mariadb_argv_is_single_execute_item():
    sql = build_drop_sql("wp_ok", "wp_ok")
    argv = build_mariadb_argv(sql)
    assert argv[:4] == ["mariadb", "--protocol=socket", "--user=root", "--batch"]
    assert argv[4] == "--execute"
    assert argv[5] == sql and len(argv) == 6


def test_import_user_is_limited_to_one_schema():
    sql = build_restricted_user_sql("shop_db", "hosty_import_abcdef123456", "a" * 48)
    assert "GRANT ALL PRIVILEGES ON `shop_db`.*" in sql
    assert " ON *.*" not in sql
    assert "root" not in sql


def test_reset_password_sql_snapshot():
    from app.services.mariadb import build_reset_password_sql

    assert build_reset_password_sql("wp_a_1b2c3d", "b" * 16) == (
        f"ALTER USER 'wp_a_1b2c3d'@'localhost' IDENTIFIED BY '{'b' * 16}'; FLUSH PRIVILEGES;"
    )


def test_show_databases_argv():
    from app.services.mariadb import build_show_databases_argv

    argv = build_show_databases_argv()
    assert argv[0] == "mariadb" and argv[-1] == "SHOW DATABASES;"
    assert "--skip-column-names" in argv
