from __future__ import annotations

import pytest

from app.system import runner, users


def test_useradd_argv_exact():
    assert users.build_useradd_argv("site-blog") == [
        "useradd",
        "--create-home",
        "--home-dir",
        "/home/site-blog",
        "--shell",
        "/usr/sbin/nologin",
        "--comment",
        "hosty site user",
        "site-blog",
    ]


def test_userdel_argv_exact():
    assert users.build_userdel_argv("site-blog") == ["userdel", "--remove", "site-blog"]


@pytest.mark.parametrize(
    "bad",
    [
        "root",
        "www-data",
        "admin",
        "site-",  # no suffix
        "site-A",  # uppercase
        "Site-foo",  # uppercase prefix
        "site-foo bar",  # whitespace
        "site-foo;rm",  # shell metachar
        "site-$(id)",  # injection attempt
        "../site-x",  # traversal
        "site--" + "a" * 40,  # too long
        "prefix-site-x",  # prefix not at start
    ],
)
def test_invalid_site_usernames_rejected(bad):
    with pytest.raises(users.InvalidSiteUserError):
        users.validate_site_username(bad)


@pytest.mark.parametrize("good", ["site-blog", "site-my-app2", "site-x9", "site-a-b-c"])
def test_valid_site_usernames_accepted(good):
    assert users.validate_site_username(good) == good


async def test_create_is_idempotent_when_user_exists(monkeypatch):
    async def fake_run(argv, **kwargs):
        if argv[0] == "id":
            return runner.CommandResult(tuple(argv), 0, "1001\n", "", 1.0)
        raise AssertionError(f"useradd must not run for an existing user: {argv}")

    monkeypatch.setattr(users.runner, "run", fake_run)
    assert await users.create("site-blog") is False


async def test_delete_is_idempotent_when_user_missing(monkeypatch):
    async def fake_run(argv, **kwargs):
        if argv[0] == "id":
            return runner.CommandResult(tuple(argv), 1, "", "no such user", 1.0)
        raise AssertionError(f"userdel must not run for a missing user: {argv}")

    monkeypatch.setattr(users.runner, "run", fake_run)
    assert await users.delete("site-blog") is False


async def test_create_raises_on_failure(monkeypatch):
    async def fake_run(argv, **kwargs):
        if argv[0] == "id":
            return runner.CommandResult(tuple(argv), 1, "", "", 1.0)
        return runner.CommandResult(tuple(argv), 1, "", "permission denied", 1.0)

    monkeypatch.setattr(users.runner, "run", fake_run)
    with pytest.raises(users.UserOperationError):
        await users.create("site-blog")


@pytest.mark.vm
async def test_create_and_delete_real_user():
    """Runs only on the privileged dev VM: pytest -m vm (requires root)."""
    name = "site-hostytest"
    assert await users.create(name) is True
    assert await users.exists(name) is True
    assert await users.delete(name) is True
    assert await users.exists(name) is False
