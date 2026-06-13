"""Stack host-fs seam (v2/M3): unit-dir sync by content marker, env files,
volume dirs, scans. Real filesystem, rooted in tmp_path by monkeypatching
the two path builders (the validation logic stays real)."""

from __future__ import annotations

import os
import sys

import pytest

from app.domain.specs import ServiceSpec, StackSpec
from app.domain.validate import SpecValidationError
from app.system import quadlet, stackhost


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    unit_root = tmp_path / "units"
    home_root = tmp_path / "home"

    def unit_dir(uid: int) -> str:
        assert isinstance(uid, int)
        return str(unit_root / str(uid))

    def stacks_root(tenant: str) -> str:
        return str(home_root / tenant / "stacks")

    monkeypatch.setattr(quadlet, "unit_dir", unit_dir)
    monkeypatch.setattr(quadlet, "stacks_root", stacks_root)
    # chown needs root and real users — record instead.
    chowns: list[str] = []
    monkeypatch.setattr(
        stackhost.shutil, "chown", lambda path, user=None, group=None: chowns.append(str(path))
    )
    return tmp_path, chowns


def spec(name="blog", services=("web",)) -> StackSpec:
    return StackSpec(
        name=name,
        tenant="hosty-t-7",
        loopback_ip="127.1.0.1",
        services=tuple(
            ServiceSpec(name=svc, image="nginx:1.27", env=(("K", "v"),)) for svc in services
        ),
    )


def test_sync_writes_the_full_set_and_reports_change(rooted):
    stack = spec()
    assert stackhost.sync_units(42, "blog", quadlet.unit_files(stack)) is True
    files = stackhost.scan_units(42)
    assert {f.file_name for f in files} == {"blog-web.container", "blog.network"}
    # Unchanged content: no-op.
    assert stackhost.sync_units(42, "blog", quadlet.unit_files(stack)) is False


def test_sync_tracks_and_removes_build_units(rooted):
    built = StackSpec(
        name="blog",
        tenant="hosty-t-7",
        loopback_ip="127.1.0.1",
        services=(
            ServiceSpec(
                name="web",
                image="hosty-build-target",
                build_repo="https://github.com/example/app.git",
                build_branch="main",
            ),
        ),
    )
    assert stackhost.sync_units(42, "blog", quadlet.unit_files(built)) is True
    assert {f.file_name for f in stackhost.scan_units(42)} == {
        "blog-web.build",
        "blog-web.container",
        "blog.network",
    }
    assert stackhost.sync_units(42, "blog", quadlet.unit_files(spec())) is True
    assert "blog-web.build" not in {f.file_name for f in stackhost.scan_units(42)}


def test_sync_removes_stale_stack_files_only(rooted):
    two = spec(services=("web", "worker"))
    stackhost.sync_units(42, "blog", quadlet.unit_files(two))
    other = spec(name="shop")
    stackhost.sync_units(42, "shop", quadlet.unit_files(other))

    one = spec(services=("web",))
    assert stackhost.sync_units(42, "blog", quadlet.unit_files(one)) is True
    names = {f.file_name for f in stackhost.scan_units(42)}
    assert "blog-worker.container" not in names
    assert {"blog-web.container", "shop-web.container", "shop.network"} <= names


def test_remove_units_only_touches_marked_files(rooted, tmp_path):
    stackhost.sync_units(42, "blog", quadlet.unit_files(spec()))
    foreign = tmp_path / "units" / "42" / "manual.container"
    foreign.write_text("[Container]\nImage=x\n", encoding="utf-8")

    assert stackhost.remove_units(42, "blog") is True
    assert foreign.exists()  # foreign file untouched
    assert stackhost.scan_units(42) == ()  # unmarked file is not reported either


def test_hyphenated_names_cannot_cross_stacks(rooted):
    """stack 'a' + service 'b-web' vs stack 'a-b' + service 'web': both render
    `a-b-web.container`-adjacent names; markers keep ownership exact."""
    stack_a = spec(name="a", services=("b-web",))
    stack_ab = spec(name="a-b", services=("web",))
    stackhost.sync_units(42, "a", quadlet.unit_files(stack_a))
    stackhost.sync_units(42, "a-b", quadlet.unit_files(stack_ab))
    # Removing stack "a" must not touch "a-b"'s files.
    stackhost.remove_units(42, "a")
    remaining = {f.stack for f in stackhost.scan_units(42)}
    assert remaining == {"a-b"}


def test_scan_units_attributes_by_marker(rooted):
    stackhost.sync_units(42, "blog", quadlet.unit_files(spec()))
    by_name = {f.file_name: f for f in stackhost.scan_units(42)}
    container = by_name["blog-web.container"]
    assert container.stack == "blog" and container.service == "web"
    assert container.spec_hash is not None
    network = by_name["blog.network"]
    assert network.stack == "blog" and network.service is None and network.spec_hash is None


def test_env_files_written_0600_inside_stacks_root(rooted, tmp_path):
    stack = spec()
    stackhost.write_env_files("hosty-t-7", quadlet.env_files(stack))
    env_path = tmp_path / "home" / "hosty-t-7" / "stacks" / "blog" / "env" / "web.env"
    assert env_path.read_text(encoding="utf-8") == "K=v\n"
    if sys.platform != "win32":
        assert oct(os.stat(env_path).st_mode & 0o777) == "0o600"


def test_env_files_are_exactly_reconciled_and_removed(rooted, tmp_path):
    first = spec(services=("web", "worker"))
    files = quadlet.env_files(first)
    assert stackhost.sync_env_files("hosty-t-7", "blog", files) is True

    second = spec(services=("web",))
    assert stackhost.sync_env_files("hosty-t-7", "blog", quadlet.env_files(second)) is True
    env_dir = tmp_path / "home" / "hosty-t-7" / "stacks" / "blog" / "env"
    assert sorted(path.name for path in env_dir.iterdir()) == ["web.env"]

    assert stackhost.remove_env_files("hosty-t-7", "blog") is True
    assert not env_dir.exists()


def test_env_file_path_escape_rejected(rooted):
    with pytest.raises(stackhost.StackHostError):
        stackhost.sync_env_files("hosty-t-7", "blog", {"/etc/passwd": "x"})
    with pytest.raises(stackhost.StackHostError):
        root = quadlet.stacks_root("hosty-t-7")
        stackhost.sync_env_files("hosty-t-7", "blog", {f"{root}/../escape.env": "x"})


def test_volume_dirs_lifecycle_and_scan(rooted, tmp_path):
    stackhost.ensure_volume_dir("hosty-t-7", "blog", "content")
    stackhost.ensure_volume_dir("hosty-t-7", "blog", "uploads")
    assert stackhost.scan_volume_dirs("hosty-t-7") == {"blog": frozenset({"content", "uploads"})}
    scan = stackhost.scan_tenant(42, "hosty-t-7")
    assert scan.volume_dirs["blog"] == frozenset({"content", "uploads"})


@pytest.mark.skipif(sys.platform == "win32", reason="rm -rf via runner needs POSIX")
async def test_remove_volume_dir_removes_data(rooted, tmp_path):
    stackhost.ensure_volume_dir("hosty-t-7", "blog", "content")
    target = tmp_path / "home" / "hosty-t-7" / "stacks" / "blog" / "volumes" / "content"
    (target / "file.txt").write_text("data", encoding="utf-8")
    await stackhost.remove_volume_dir("hosty-t-7", "blog", "content")
    assert not target.exists()


def test_grammar_rejected_before_any_path_is_built(rooted):
    with pytest.raises(SpecValidationError):
        stackhost.sync_units(42, "Blog", {})
    with pytest.raises(SpecValidationError):
        stackhost.ensure_volume_dir("hosty-t-7", "blog", "../etc")
