"""WordPress service: argv builders (runuser as site user), validation, status."""

from __future__ import annotations

import pytest

from app.services import wordpress
from app.system.runner import CommandResult
from app.system.users import InvalidSiteUserError

SITE_USER = "site-example-com-a1b2c3"
DOC_ROOT = "/var/www/example.com/public_html"


def test_build_wp_argv_runs_as_site_user_never_root():
    argv = wordpress.build_wp_argv(SITE_USER, DOC_ROOT, ["core", "version"])
    assert argv == [
        "runuser",
        "-u",
        SITE_USER,
        "--",
        "wp",
        "core",
        "version",
        f"--path={DOC_ROOT}",
    ]


def test_build_wp_argv_rejects_non_site_users():
    for user in ["root", "www-data", "site-", "admin"]:
        with pytest.raises(InvalidSiteUserError):
            wordpress.build_wp_argv(user, DOC_ROOT, ["core", "version"])


def test_build_wp_argv_keeps_arguments_as_single_items():
    argv = wordpress.build_wp_argv(
        SITE_USER, DOC_ROOT, ["core", "install", "--title=My Site; rm -rf /"]
    )
    assert "--title=My Site; rm -rf /" in argv  # one argv item, never a shell string


@pytest.mark.parametrize("locale", ["en_US", "de_DE", "fr_FR", "ja", "pt_BR"])
def test_valid_locales(locale):
    assert wordpress.validate_locale(locale) == locale


@pytest.mark.parametrize("locale", ["", "en-US", "EN_us", "english", "en_US; rm", "../x"])
def test_invalid_locales(locale):
    with pytest.raises(wordpress.InvalidWpArgumentError):
        wordpress.validate_locale(locale)


@pytest.mark.parametrize("version", ["latest", "6.5", "6.5.1", "10.0"])
def test_valid_versions(version):
    assert wordpress.validate_wp_version(version) == version


@pytest.mark.parametrize("version", ["", "six", "6", "6.5.1.2", "6.5;x", "v6.5"])
def test_invalid_versions(version):
    with pytest.raises(wordpress.InvalidWpArgumentError):
        wordpress.validate_wp_version(version)


def _result(stdout: str = "", ok: bool = True) -> CommandResult:
    return CommandResult(
        argv=("wp",), returncode=0 if ok else 1, stdout=stdout, stderr="", duration_ms=1.0
    )


class FakeSite:
    site_user = SITE_USER
    doc_root = DOC_ROOT


async def test_status_not_installed(monkeypatch):
    async def fake_run_wp(user, root, args, **kw):
        assert args == ["core", "is-installed"]
        return _result(ok=False)

    monkeypatch.setattr(wordpress, "run_wp", fake_run_wp)
    status = await wordpress.status(FakeSite())
    assert status == wordpress.WpStatus(installed=False)


async def test_status_installed_with_update(monkeypatch):
    responses = {
        ("core", "is-installed"): _result(ok=True),
        ("core", "version"): _result("6.5.1"),
        ("core", "check-update"): _result('[{"version":"6.6","update_type":"major"}]'),
        ("plugin", "list"): _result("4"),
        ("theme", "list"): _result("2"),
    }

    async def fake_run_wp(user, root, args, **kw):
        return responses[tuple(args[:2])]

    monkeypatch.setattr(wordpress, "run_wp", fake_run_wp)
    status = await wordpress.status(FakeSite())
    assert status == wordpress.WpStatus(
        installed=True,
        version="6.5.1",
        update_available="6.6",
        plugin_count=4,
        theme_count=2,
    )


async def test_run_action_rejects_unknown_action():
    with pytest.raises(wordpress.InvalidWpArgumentError):
        await wordpress.run_action(FakeSite(), "drop_tables")


async def test_update_core_runs_update_then_db(monkeypatch):
    calls: list[list[str]] = []

    async def fake_run_wp(user, root, args, **kw):
        calls.append(args)
        return _result()

    monkeypatch.setattr(wordpress, "run_wp", fake_run_wp)
    assert await wordpress.run_action(FakeSite(), "update_core") is None
    assert calls == [["core", "update"], ["core", "update-db"]]
