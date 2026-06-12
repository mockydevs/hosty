"""Tenant Linux-user seam (v2/M0): grammar is structurally incapable of
naming a non-tenant account; argv builders are exact; ledger range policy
is pure and disjoint."""

from __future__ import annotations

import pytest

from app.system import tenants


@pytest.mark.parametrize("name", ["hosty-t-1", "hosty-t-42", "hosty-t-9999999999"])
def test_valid_tenant_usernames(name):
    assert tenants.validate_tenant_username(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "root",
        "site-blog-abc123",
        "hosty-t-",
        "hosty-t-1x",
        "hosty-t--1",
        "hosty-t-1\n",
        "HOSTY-T-1",
        "hosty-t-12345678901",  # > 10 digits
        "",
        None,
        7,
    ],
)
def test_invalid_tenant_usernames_rejected(name):
    with pytest.raises(tenants.InvalidTenantUserError):
        tenants.validate_tenant_username(name)


def test_linux_user_for_derives_from_user_id():
    assert tenants.linux_user_for(7) == "hosty-t-7"
    assert tenants.home_dir_for("hosty-t-7") == "/home/hosty-t-7"


def test_subid_ranges_are_disjoint_and_above_base():
    seen: list[tuple[int, int]] = [tenants.subid_range_for(i) for i in range(50)]
    for i, (start, count) in enumerate(seen):
        assert start >= tenants.SUBID_BASE
        assert count == tenants.SUBID_COUNT
        for start2, count2 in seen[i + 1 :]:
            assert start + count <= start2 or start2 + count2 <= start  # disjoint


@pytest.mark.parametrize("index", [-1, 1.5, "0", True, None])
def test_invalid_range_index_rejected(index):
    with pytest.raises(tenants.InvalidTenantUserError):
        tenants.subid_range_for(index)


def test_useradd_argv():
    assert tenants.build_useradd_argv("hosty-t-7") == [
        "useradd",
        "--create-home",
        "--home-dir",
        "/home/hosty-t-7",
        "--shell",
        "/usr/sbin/nologin",
        "--comment",
        "hosty tenant",
        "hosty-t-7",
    ]


def test_subid_argvs_are_exact_inclusive_ranges():
    argv = tenants.build_add_subuids_argv("hosty-t-7", 1_000_000, 65536)
    assert argv == ["usermod", "--add-subuids", "1000000-1065535", "hosty-t-7"]
    argv = tenants.build_add_subgids_argv("hosty-t-7", 1_065_536, 65536)
    assert argv == ["usermod", "--add-subgids", "1065536-1131071", "hosty-t-7"]


def test_subid_argv_rejects_ranges_below_base_or_empty():
    with pytest.raises(tenants.InvalidTenantUserError):
        tenants.build_add_subuids_argv("hosty-t-7", 100, 65536)  # collides with users
    with pytest.raises(tenants.InvalidTenantUserError):
        tenants.build_add_subuids_argv("hosty-t-7", 1_000_000, 0)
    with pytest.raises(tenants.InvalidTenantUserError):
        tenants.build_add_subuids_argv("hosty-t-7", True, 65536)


def test_linger_argv():
    assert tenants.build_linger_argv("hosty-t-7") == ["loginctl", "enable-linger", "hosty-t-7"]
    assert tenants.build_linger_argv("hosty-t-7", enable=False) == [
        "loginctl",
        "disable-linger",
        "hosty-t-7",
    ]


def test_builders_reject_injection_through_name():
    for builder in (
        tenants.build_useradd_argv,
        tenants.build_userdel_argv,
        tenants.build_uid_argv,
    ):
        with pytest.raises(tenants.InvalidTenantUserError):
            builder("--root=/tmp")
