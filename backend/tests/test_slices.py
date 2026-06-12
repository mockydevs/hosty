"""systemd slices (Phase 11c): pure rendering + idempotent install/remove."""

from __future__ import annotations

import pytest

from app.system import slices
from app.system.users import InvalidSiteUserError


def test_render_slice_snapshot():
    assert slices.render_slice("site-blog", cpu_quota_percent=150, memory_max_mb=512) == (
        "# Managed by HostyPanel — do not edit by hand.\n"
        "[Unit]\n"
        "Description=HostyPanel resource slice for site-blog\n"
        "\n"
        "[Slice]\n"
        "CPUQuota=150%\n"
        "MemoryMax=512M\n"
    )


def test_render_slice_partial_limits():
    out = slices.render_slice("site-blog", cpu_quota_percent=None, memory_max_mb=256)
    assert "CPUQuota" not in out
    assert "MemoryMax=256M" in out


def test_render_slice_validates_ranges():
    with pytest.raises(ValueError):
        slices.render_slice("site-blog", cpu_quota_percent=0, memory_max_mb=None)
    with pytest.raises(ValueError):
        slices.render_slice("site-blog", cpu_quota_percent=1601, memory_max_mb=None)
    with pytest.raises(ValueError):
        slices.render_slice("site-blog", cpu_quota_percent=None, memory_max_mb=8)


def test_slice_name_rejects_bad_usernames():
    with pytest.raises(InvalidSiteUserError):
        slices.slice_name("root; rm -rf /")


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    async def run(self, argv, timeout=None):
        self.calls.append(list(argv))

        class R:
            ok = True
            stdout = ""

        return R()


@pytest.fixture
def fake_system(monkeypatch):
    fake = FakeRunner()

    async def fake_run(argv, timeout=None):
        return await fake.run(argv, timeout=timeout)

    async def fake_control(action, unit):
        fake.calls.append(["systemctl", action, unit])

    monkeypatch.setattr(slices.runner, "run", fake_run)
    monkeypatch.setattr(slices.systemd, "control", fake_control)
    return fake


async def test_install_slice_writes_unit_and_reloads(tmp_path, fake_system):
    installed = await slices.install_slice(
        "site-blog", cpu_quota_percent=100, memory_max_mb=256, slice_dir=str(tmp_path)
    )
    assert installed is True
    content = (tmp_path / "hosty-site-blog.slice").read_text(encoding="utf-8")
    assert "CPUQuota=100%" in content
    assert ["systemctl", "daemon-reload"] in fake_system.calls


async def test_install_slice_is_idempotent(tmp_path, fake_system):
    kwargs = dict(cpu_quota_percent=100, memory_max_mb=256, slice_dir=str(tmp_path))
    await slices.install_slice("site-blog", **kwargs)
    fake_system.calls.clear()
    assert await slices.install_slice("site-blog", **kwargs) is True
    assert fake_system.calls == []  # desired state already — no reload


async def test_install_slice_with_no_limits_removes_existing(tmp_path, fake_system):
    await slices.install_slice(
        "site-blog", cpu_quota_percent=100, memory_max_mb=None, slice_dir=str(tmp_path)
    )
    installed = await slices.install_slice(
        "site-blog", cpu_quota_percent=None, memory_max_mb=None, slice_dir=str(tmp_path)
    )
    assert installed is False
    assert not (tmp_path / "hosty-site-blog.slice").exists()


async def test_remove_slice_is_idempotent(tmp_path, fake_system):
    assert await slices.remove_slice("site-blog", slice_dir=str(tmp_path)) is False
    await slices.install_slice(
        "site-blog", cpu_quota_percent=100, memory_max_mb=None, slice_dir=str(tmp_path)
    )
    assert await slices.remove_slice("site-blog", slice_dir=str(tmp_path)) is True
    assert await slices.remove_slice("site-blog", slice_dir=str(tmp_path)) is False
