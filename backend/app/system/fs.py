"""Filesystem mutations for site directories.

Safety invariant: every mutating operation validates that its target lies
strictly inside the configured sites root (no traversal, no symlink tricks at
the path level), so this layer cannot touch arbitrary host paths.
"""

from __future__ import annotations

import os
import posixpath
import stat
from pathlib import Path

from app.system import runner
from app.system.users import validate_site_username


class InvalidSitePathError(runner.InvalidCommandError):
    pass


class FsOperationError(RuntimeError):
    pass


def validate_site_path(path: str, *, root: str) -> str:
    """Return `path` if it is a normalized absolute path strictly inside `root`."""
    if not isinstance(path, str) or "\x00" in path:
        raise InvalidSitePathError(f"Invalid site path: {path!r}")
    if root.startswith("/"):
        if not path.startswith("/"):
            raise InvalidSitePathError(f"Invalid site path: {path!r}")
        normalized = posixpath.normpath(path)
        if normalized != path.rstrip("/") or ".." in path.split("/"):
            raise InvalidSitePathError(f"Path must be normalized, got {path!r}")
        root_norm = posixpath.normpath(root)
        if not normalized.startswith(root_norm + "/") or normalized == root_norm:
            raise InvalidSitePathError(f"Path {path!r} is outside the sites root {root!r}")
        return normalized
    if not os.path.isabs(path):
        raise InvalidSitePathError(f"Invalid site path: {path!r}")
    else:
        normalized = os.path.normpath(path)
        root_norm = os.path.normpath(root)
        outside_root = os.path.commonpath([normalized, root_norm]) != root_norm
        if normalized != path.rstrip("/\\") or outside_root:
            raise InvalidSitePathError(f"Path {path!r} is outside the sites root {root!r}")
        if normalized == root_norm:
            raise InvalidSitePathError(f"Path {path!r} is outside the sites root {root!r}")
        return normalized
        return normalized


def assert_no_symlink(path: str, *, root: str) -> str:
    """Require every existing path component from root through path to be real."""
    target = validate_site_path(path, root=root)
    windows_style = not root.startswith("/")
    root_norm = os.path.normpath(root) if windows_style else posixpath.normpath(root)
    if os.path.islink(root_norm):
        raise InvalidSitePathError(f"Sites root must not be a symlink: {root_norm}")
    current = root_norm
    relative = (
        os.path.relpath(target, root_norm)
        if windows_style
        else posixpath.relpath(target, root_norm)
    )
    components = relative.split(os.sep) if windows_style else relative.split("/")
    for component in components:
        current = (
            os.path.join(current, component)
            if windows_style
            else posixpath.join(current, component)
        )
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise InvalidSitePathError(f"Site path contains a symlink: {current}")
    return target


def build_mkdir_argv(path: str, *, root: str) -> list[str]:
    return ["mkdir", "-p", validate_site_path(path, root=root)]


def build_chown_argv(user: str, path: str, *, root: str) -> list[str]:
    validate_site_username(user)
    return ["chown", "-R", f"{user}:{user}", validate_site_path(path, root=root)]


def build_chmod_argv(mode: str, path: str, *, root: str) -> list[str]:
    if mode not in {"750", "755", "2750", "2755"}:
        raise InvalidSitePathError(f"Disallowed mode: {mode!r}")
    return ["chmod", mode, validate_site_path(path, root=root)]


def build_rmtree_argv(path: str, *, root: str) -> list[str]:
    # `--` stops option parsing; the path is already validated to be inside root.
    return ["rm", "-rf", "--", validate_site_path(path, root=root)]


def build_rsync_argv(src_dir: str, dst_dir: str, *, root: str, delete: bool = False) -> list[str]:
    """Pure: mirror `src_dir`'s contents into `dst_dir` (both validated inside
    the sites root). `delete` removes files in dst missing from src (Phase 11d
    staging push-back)."""
    src = validate_site_path(src_dir, root=root)
    dst = validate_site_path(dst_dir, root=root)
    argv = ["rsync", "-a", "--safe-links"]
    if delete:
        argv.append("--delete")
    argv.extend(["--", f"{src}/", f"{dst}/"])
    return argv


async def _run_or_raise(argv: list[str], what: str, *, timeout: float = 60) -> None:
    result = await runner.run(argv, timeout=timeout)
    if not result.ok:
        raise FsOperationError(f"{what} failed: {result.stderr.strip() or result.stdout.strip()}")


async def create_dir(path: str, *, root: str) -> None:
    await _run_or_raise(build_mkdir_argv(path, root=root), f"mkdir {path}")


async def chown_recursive(user: str, path: str, *, root: str) -> None:
    await _run_or_raise(build_chown_argv(user, path, root=root), f"chown {path}")


async def secure_site_layout(
    site_dir: str, doc_root: str, user: str, *, root: str, create: bool = True
) -> None:
    """Establish the non-replaceable, private per-site directory boundary."""
    validate_site_username(user)
    site_dir = validate_site_path(site_dir, root=root)
    doc_root = validate_site_path(doc_root, root=root)
    if posixpath.dirname(doc_root) != site_dir:
        raise InvalidSitePathError("Document root must be an immediate child of the site directory")
    if create:
        await create_dir(doc_root, root=root)
    assert_no_symlink(site_dir, root=root)
    assert_no_symlink(doc_root, root=root)
    commands = [
        ["chown", f"root:{user}", site_dir],
        ["chmod", "710", site_dir],
        ["chown", "-R", f"{user}:{user}", doc_root],
        ["find", doc_root, "-type", "d", "-exec", "chmod", "750", "{}", "+"],
        ["find", doc_root, "-type", "f", "-exec", "chmod", "640", "{}", "+"],
        ["setfacl", "-m", "u:caddy:--x", site_dir],
        ["setfacl", "-m", "u:hosty-filebrowser:--x", site_dir],
        ["setfacl", "-R", "-m", "u:caddy:r-X,m::r-X,o::---", doc_root],
        ["setfacl", "-R", "-m", "u:hosty-filebrowser:rwx,m::rwx,o::---", doc_root],
        ["setfacl", "-m", "d:u:caddy:r-X,d:m::r-X,d:o::---", doc_root],
        ["setfacl", "-m", "d:u:hosty-filebrowser:rwx,d:m::rwx,d:o::---", doc_root],
    ]
    for command in commands:
        await _run_or_raise(command, f"secure site layout {site_dir}")
    assert_no_symlink(doc_root, root=root)


async def remove_tree(path: str, *, root: str) -> None:
    """Idempotent: removing a tree that does not exist succeeds (rm -rf)."""
    await _run_or_raise(build_rmtree_argv(path, root=root), f"rm -rf {path}")


async def mirror_tree(src_dir: str, dst_dir: str, *, root: str, delete: bool = False) -> None:
    assert_no_symlink(src_dir, root=root)
    assert_no_symlink(dst_dir, root=root)
    await _run_or_raise(
        build_rsync_argv(src_dir, dst_dir, root=root, delete=delete),
        f"rsync {src_dir} -> {dst_dir}",
        timeout=1800,
    )


async def mirror_import_tree(
    src_dir: str, dst_dir: str, *, import_root: str, sites_root: str
) -> None:
    raw_source = Path(src_dir)
    if raw_source.is_symlink():
        raise InvalidSitePathError("Import staging path must not be a symlink")
    source = raw_source.resolve(strict=True)
    trusted_root = Path(import_root).resolve(strict=True)
    try:
        source.relative_to(trusted_root)
    except ValueError as exc:
        raise InvalidSitePathError("Import staging directory escaped the uploads root") from exc
    if source.is_symlink() or not source.is_dir():
        raise InvalidSitePathError("Import staging path must be a real directory")
    destination = assert_no_symlink(dst_dir, root=sites_root)
    await _run_or_raise(
        ["rsync", "-a", "--safe-links", "--delete", "--", f"{source}/", f"{destination}/"],
        f"import {source} -> {destination}",
        timeout=1800,
    )


def write_file(path: str, content: str, *, root: str) -> None:
    """Write a text file inside the sites root (no shell involved)."""
    target = assert_no_symlink(path, root=root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
