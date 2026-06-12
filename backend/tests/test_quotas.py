"""Effective limit resolution (Phase 11c/11d): plan defaults, user overrides."""

from __future__ import annotations

from app.db.models import Plan, User
from app.services import quotas


def _user(**kwargs) -> User:
    defaults = dict(
        username="alice",
        password_hash="x",
        role="client",
        max_sites=None,
        max_databases=None,
        max_disk_mb=None,
        cpu_quota_percent=None,
        memory_max_mb=None,
    )
    defaults.update(kwargs)
    return User(**defaults)


def _plan(**kwargs) -> Plan:
    defaults = dict(
        name="starter",
        max_sites=1,
        max_databases=2,
        max_disk_mb=1024,
        cpu_quota_percent=100,
        memory_max_mb=512,
    )
    defaults.update(kwargs)
    return Plan(**defaults)


def test_no_plan_no_overrides_is_unlimited():
    assert quotas.resolve(_user(), None) == quotas.UNLIMITED


def test_plan_supplies_defaults():
    limits = quotas.resolve(_user(), _plan())
    assert limits.max_sites == 1
    assert limits.max_databases == 2
    assert limits.max_disk_mb == 1024
    assert limits.cpu_quota_percent == 100
    assert limits.memory_max_mb == 512


def test_explicit_user_values_override_plan():
    limits = quotas.resolve(_user(max_sites=5, memory_max_mb=2048), _plan())
    assert limits.max_sites == 5  # user override wins
    assert limits.memory_max_mb == 2048
    assert limits.max_databases == 2  # plan default still applies


def test_user_overrides_without_plan():
    limits = quotas.resolve(_user(max_databases=3), None)
    assert limits.max_databases == 3
    assert limits.max_sites is None


def test_admin_is_never_limited():
    admin = _user(username="root", role="admin", max_sites=1)
    assert quotas.resolve(admin, _plan()) == quotas.UNLIMITED
