"""Filesystem mutations for site directories.

Safety invariant: every mutating operation validates that its target lies
strictly inside the configured sites root (no traversal, no symlink tricks at
the path level), so this layer cannot touch arbitrary host paths.
"""

from __future__ import annotations

import posixpath

from app.system import runner
from app.system.users import validate_site_username


class InvalidSitePathError(runner.InvalidCommandError):
    pass


class FsOperationError(RuntimeError):
    pass


def validate_site_path(path: str, *, root: str) -> str:
    """Return `path` if it is a normalized absolute path strictly inside `root`."""
    if not isinstance(path, str) or "\x00" in path or not path.startswith("/"):
        raise InvalidSitePathError(f"Invalid site path: {path!r}")
    normalized = posixpath.normpath(path)
    if normalized != path.rstrip("/") or ".." in path.split("/"):
        raise InvalidSitePathError(f"Path must be normalized, got {path!r}")
    root_norm = posixpath.normpath(root)
    if not normalized.startswith(root_norm + "/") or normalized == root_norm:
        raise InvalidSitePathError(f"Path {path!r} is outside the sites root {root!r}")
    return normalized


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
    argv = ["rsync", "-a"]
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


async def remove_tree(path: str, *, root: str) -> None:
    """Idempotent: removing a tree that does not exist succeeds (rm -rf)."""
    await _run_or_raise(build_rmtree_argv(path, root=root), f"rm -rf {path}")


async def mirror_tree(src_dir: str, dst_dir: str, *, root: str, delete: bool = False) -> None:
    await _run_or_raise(
        build_rsync_argv(src_dir, dst_dir, root=root, delete=delete),
        f"rsync {src_dir} -> {dst_dir}",
        timeout=1800,
    )


def write_file(path: str, content: str, *, root: str) -> None:
    """Write a text file inside the sites root (no shell involved)."""
    target = validate_site_path(path, root=root)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
