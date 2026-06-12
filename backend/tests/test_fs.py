"""system/fs: path validation (traversal/injection impossible) + argv builders."""

from __future__ import annotations

import pytest

from app.system import fs
from app.system.fs import (
    InvalidSitePathError,
    assert_no_symlink,
    build_chown_argv,
    build_mkdir_argv,
    build_rmtree_argv,
    validate_site_path,
)

ROOT = "/var/www"


@pytest.mark.parametrize(
    "path",
    [
        "/var/www/example.com",
        "/var/www/example.com/public_html",
        "/var/www/xn--bcher-kva.example/public_html",
    ],
)
def test_valid_paths(path):
    assert validate_site_path(path, root=ROOT) == path


@pytest.mark.parametrize(
    "path",
    [
        "/var/www",  # the root itself
        "/var/www/",  # root with slash
        "/etc/passwd",  # outside root
        "/var/www/../etc",  # traversal
        "/var/www/site/../../etc",  # nested traversal
        "var/www/site",  # relative
        "/var/wwwevil/site",  # prefix trick
        "/var/www/site\x00",  # NUL byte
        "",  # empty
        "/var/www//site",  # not normalized
    ],
)
def test_invalid_paths(path):
    with pytest.raises(InvalidSitePathError):
        validate_site_path(path, root=ROOT)


def test_mkdir_argv():
    assert build_mkdir_argv("/var/www/a.com", root=ROOT) == ["mkdir", "-p", "/var/www/a.com"]


def test_chown_argv():
    assert build_chown_argv("site-a-abc123", "/var/www/a.com", root=ROOT) == [
        "chown",
        "-R",
        "site-a-abc123:site-a-abc123",
        "/var/www/a.com",
    ]


def test_chown_rejects_non_site_user():
    from app.system.users import InvalidSiteUserError

    with pytest.raises(InvalidSiteUserError):
        build_chown_argv("root", "/var/www/a.com", root=ROOT)


def test_rmtree_argv_uses_end_of_options():
    assert build_rmtree_argv("/var/www/a.com", root=ROOT) == ["rm", "-rf", "--", "/var/www/a.com"]


def test_rmtree_injection_attempts_stay_single_argv_items():
    # Shell metacharacters are harmless in argv form (never passed to a shell);
    # invalid paths are rejected outright.
    for evil in ["/var/www/a.com; rm -rf /x", "/var/www/$(reboot)", "/var/www/a\nb"]:
        try:
            argv = build_rmtree_argv(evil, root=ROOT)
        except InvalidSitePathError:
            continue
        assert argv[:3] == ["rm", "-rf", "--"] and len(argv) == 4
        assert argv[3] == validate_site_path(evil, root=ROOT)


def test_existing_symlink_component_is_rejected(tmp_path):
    root = tmp_path / "sites"
    real = tmp_path / "real"
    root.mkdir()
    real.mkdir()
    link = root / "example.com"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(InvalidSitePathError, match="symlink"):
        assert_no_symlink(str(link / "public_html"), root=str(root))


async def test_import_mirror_rejects_symlink_source_and_destination(tmp_path):
    imports = tmp_path / "imports"
    sites = tmp_path / "sites"
    source = imports / "source"
    destination = sites / "example.com" / "public_html"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    source_link = imports / "source-link"
    destination_link = sites / "linked.example"
    try:
        source_link.symlink_to(source, target_is_directory=True)
        destination_link.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(InvalidSitePathError, match="symlink"):
        await fs.mirror_import_tree(
            str(source_link),
            str(destination),
            import_root=str(imports),
            sites_root=str(sites),
        )
    with pytest.raises(InvalidSitePathError, match="symlink"):
        await fs.mirror_import_tree(
            str(source),
            str(destination_link),
            import_root=str(imports),
            sites_root=str(sites),
        )
