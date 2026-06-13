"""systemd --machine adapter (v2/M2): exact argv, grammar rejection, and
the graceful-degradation contract (status never raises; control does)."""

from __future__ import annotations

import pytest

from app.system import runner, systemd_user
from app.system.tenants import InvalidTenantUserError


def test_daemon_reload_argv():
    assert systemd_user.build_daemon_reload_argv("hosty-t-7") == [
        "systemctl",
        "--machine",
        "hosty-t-7@.host",
        "--user",
        "daemon-reload",
    ]


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_control_argv(action):
    assert systemd_user.build_control_argv("hosty-t-7", action, "blog-web.service") == [
        "systemctl",
        "--machine",
        "hosty-t-7@.host",
        "--user",
        action,
        "blog-web.service",
    ]


@pytest.mark.parametrize("action", ["enable", "disable", "kill", "isolate", ""])
def test_control_rejects_unknown_actions(action):
    with pytest.raises(systemd_user.InvalidSystemdUserArgError):
        systemd_user.build_control_argv("hosty-t-7", action, "blog-web.service")


def test_show_argv():
    assert systemd_user.build_show_argv("hosty-t-7", "blog-web.service") == [
        "systemctl",
        "--machine",
        "hosty-t-7@.host",
        "--user",
        "show",
        "blog-web.service",
        "--property=LoadState,ActiveState,SubState,UnitFileState",
        "--no-pager",
    ]


def test_is_active_argv():
    assert systemd_user.build_is_active_argv("hosty-t-7", "blog-web.service") == [
        "systemctl",
        "--machine",
        "hosty-t-7@.host",
        "--user",
        "is-active",
        "--quiet",
        "blog-web.service",
    ]


def test_journal_argv_clamps_tail():
    argv = systemd_user.build_journal_argv("hosty-t-7", "blog-web.service", tail=999999)
    assert argv == [
        "journalctl",
        "-M",
        "hosty-t-7@.host",  # connect AS the tenant TO the local host, like systemctl
        "--user-unit",
        "blog-web.service",
        "-n",
        "5000",
        "--no-pager",
        "--output",
        "short-iso",
    ]
    assert systemd_user.build_journal_argv("hosty-t-7", "blog-web.service", tail=-5)[6] == "1"


@pytest.mark.parametrize("user", ["root", "site-blog", "hosty-t-", "hosty-t-7\n", "-x"])
def test_builders_reject_non_tenant_users(user):
    with pytest.raises(InvalidTenantUserError):
        systemd_user.build_daemon_reload_argv(user)


@pytest.mark.parametrize("unit", ["-evil.service", "a b.service", "u\nnit", ""])
def test_builders_reject_bad_units(unit):
    with pytest.raises(runner.InvalidCommandError):
        systemd_user.build_control_argv("hosty-t-7", "start", unit)


async def test_status_unavailable_when_systemctl_missing(monkeypatch):
    async def boom(argv, **kwargs):
        raise runner.CommandNotFoundError("systemctl")

    monkeypatch.setattr(systemd_user.runner, "run", boom)
    result = await systemd_user.status("hosty-t-7", "blog-web.service")
    assert result.available is False
    assert result.active_state == "unknown"


async def test_status_unavailable_when_machine_unreachable(monkeypatch):
    async def fail(argv, **kwargs):
        return runner.CommandResult(tuple(argv), 1, "", "Failed to connect to bus", 1.0)

    monkeypatch.setattr(systemd_user.runner, "run", fail)
    result = await systemd_user.status("hosty-t-7", "blog-web.service")
    assert result.available is False


async def test_status_parses_show_output(monkeypatch):
    out = "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=generated\n"

    async def ok(argv, **kwargs):
        return runner.CommandResult(tuple(argv), 0, out, "", 1.0)

    monkeypatch.setattr(systemd_user.runner, "run", ok)
    result = await systemd_user.status("hosty-t-7", "blog-web.service")
    assert result.available is True
    assert result.active_state == "active"
    assert result.sub_state == "running"


async def test_control_raises_when_systemd_missing(monkeypatch):
    async def boom(argv, **kwargs):
        raise runner.CommandNotFoundError("systemctl")

    monkeypatch.setattr(systemd_user.runner, "run", boom)
    with pytest.raises(systemd_user.SystemdUserError):
        await systemd_user.control("hosty-t-7", "start", "blog-web.service")


async def test_control_raises_on_failure_with_detail(monkeypatch):
    async def fail(argv, **kwargs):
        return runner.CommandResult(tuple(argv), 1, "", "Unit not found", 1.0)

    monkeypatch.setattr(systemd_user.runner, "run", fail)
    with pytest.raises(systemd_user.SystemdUserError, match="Unit not found"):
        await systemd_user.control("hosty-t-7", "start", "blog-web.service")


async def test_is_active_and_journal(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def ok(argv, **kwargs):
        calls.append(tuple(argv))
        return runner.CommandResult(tuple(argv), 0, "log line\n", "", 1.0)

    monkeypatch.setattr(systemd_user.runner, "run", ok)
    assert await systemd_user.is_active("hosty-t-7", "blog-web.service") is True
    assert await systemd_user.journal("hosty-t-7", "blog-web.service", tail=10) == "log line\n"
    assert await systemd_user.daemon_reload("hosty-t-7") is None
    assert len(calls) == 3


async def test_is_active_false_when_unavailable(monkeypatch):
    async def boom(argv, **kwargs):
        raise runner.CommandNotFoundError("systemctl")

    monkeypatch.setattr(systemd_user.runner, "run", boom)
    assert await systemd_user.is_active("hosty-t-7", "blog-web.service") is False
