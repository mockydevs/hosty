from __future__ import annotations

import pytest

from app.system import runner, systemd


def test_build_status_argv_exact():
    assert systemd.build_status_argv("caddy") == [
        "systemctl",
        "show",
        "caddy",
        "--property=LoadState,ActiveState,SubState,UnitFileState",
        "--no-pager",
    ]


def test_build_control_argv_exact():
    assert systemd.build_control_argv("restart", "php8.3-fpm") == [
        "systemctl",
        "restart",
        "php8.3-fpm",
    ]


@pytest.mark.parametrize(
    "bad",
    ["caddy; rm -rf /", "-rf", "", "a b", "caddy\n", "$(reboot)", "../etc", "unit|x", "x" * 80],
)
def test_invalid_unit_names_rejected(bad):
    with pytest.raises(systemd.InvalidUnitError):
        systemd.validate_unit(bad)


@pytest.mark.parametrize("good", ["caddy", "php8.3-fpm", "mariadb", "pdns", "user@1000.service"])
def test_valid_unit_names_accepted(good):
    assert systemd.validate_unit(good) == good


def test_unknown_action_rejected():
    with pytest.raises(systemd.InvalidUnitError):
        systemd.build_control_argv("kill-everything", "caddy")


def test_parse_show_output_loaded():
    out = "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n"
    s = systemd.parse_show_output("caddy", out)
    assert s == systemd.ServiceStatus("caddy", True, "active", "running", "enabled")


def test_parse_show_output_not_found():
    out = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\n"
    s = systemd.parse_show_output("ghost", out)
    assert s.available is False
    assert s.active_state == "inactive"


async def test_status_degrades_when_systemctl_missing(monkeypatch):
    async def fake_run(argv, **kwargs):
        raise runner.CommandNotFoundError("no systemctl")

    monkeypatch.setattr(systemd.runner, "run", fake_run)
    s = await systemd.status("caddy")
    assert s.available is False
    assert s.unit == "caddy"


async def test_control_raises_on_failure(monkeypatch):
    async def fake_run(argv, **kwargs):
        return runner.CommandResult(tuple(argv), 1, "", "boom", 1.0)

    monkeypatch.setattr(systemd.runner, "run", fake_run)
    with pytest.raises(systemd.SystemdError):
        await systemd.control("restart", "caddy")


async def test_control_returns_fresh_status_on_success(monkeypatch):
    calls = []

    async def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "restart":
            return runner.CommandResult(tuple(argv), 0, "", "", 1.0)
        return runner.CommandResult(
            tuple(argv),
            0,
            "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n",
            "",
            1.0,
        )

    monkeypatch.setattr(systemd.runner, "run", fake_run)
    s = await systemd.control("restart", "caddy")
    assert s.active_state == "active"
    assert calls[0][1] == "restart"
    assert calls[1][1] == "show"
