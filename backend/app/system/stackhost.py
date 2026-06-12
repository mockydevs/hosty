"""Host filesystem seam for stacks (v2/M3, ADR-013): quadlet unit-dir sync,
tenant-owned env files, volume directories, and the read side the observer
uses. The ONLY module that touches `/etc/containers/systemd/users/<uid>/`
or `~tenant/stacks/`.

Every path is BUILT from grammar-validated parts (tenant username, stack/
service/volume slugs, integer uid) — no caller-supplied paths exist, so
traversal is impossible by construction. File ownership is attributed by
the `# hosty-stack=` content marker, never by filename parsing (slugs may
contain hyphens, making filenames ambiguous).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.validate import validate_slug
from app.system import quadlet, runner
from app.system.tenants import validate_tenant_username

UNIT_SUFFIXES = (".container", ".network")


class StackHostError(RuntimeError):
    pass


@dataclass(frozen=True)
class UnitFile:
    """One quadlet file as found on disk, attributed by content markers."""

    file_name: str
    stack: str | None
    service: str | None  # None for .network files (and corrupt markers)
    spec_hash: str | None


@dataclass(frozen=True)
class TenantScan:
    """Everything the observer needs from one tenant's host surface."""

    unit_files: tuple[UnitFile, ...] = ()
    volume_dirs: dict[str, frozenset[str]] = field(default_factory=dict)  # stack -> volumes


def _open_write(path: str, content: str, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.chmod(path, mode)  # O_CREAT mode is masked by umask; make it exact


def _stack_files_on_disk(unit_dir: Path, stack: str) -> dict[str, str]:
    """filename -> content for every unit file the marker attributes to
    `stack`. Unreadable/foreign files are skipped, never raised on."""
    found: dict[str, str] = {}
    if not unit_dir.is_dir():
        return found
    for entry in unit_dir.iterdir():
        if not entry.is_file() or entry.suffix not in UNIT_SUFFIXES:
            continue
        try:
            content = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if quadlet.read_stack_marker(content) == stack:
            found[entry.name] = content
    return found


def sync_units(uid: int, stack: str, desired: dict[str, str]) -> bool:
    """Make the unit dir hold EXACTLY `desired` for this stack: write every
    desired file, delete stack-owned files not in the set. Returns True when
    anything changed (caller decides whether daemon-reload is needed)."""
    validate_slug(stack, what="stack name")
    unit_dir = Path(quadlet.unit_dir(uid))
    unit_dir.mkdir(parents=True, exist_ok=True)
    existing = _stack_files_on_disk(unit_dir, stack)
    changed = False
    for file_name, content in desired.items():
        if existing.get(file_name) != content:
            _open_write(str(unit_dir / file_name), content, 0o644)
            changed = True
    for file_name in existing:
        if file_name not in desired:
            (unit_dir / file_name).unlink(missing_ok=True)
            changed = True
    return changed


def remove_units(uid: int, stack: str) -> bool:
    """Delete every unit file the marker attributes to the stack."""
    return sync_units(uid, stack, {})


def scan_units(uid: int) -> tuple[UnitFile, ...]:
    """All hosty-attributable quadlet files in one tenant's unit dir."""
    unit_dir = Path(quadlet.unit_dir(uid))
    files: list[UnitFile] = []
    if not unit_dir.is_dir():
        return ()
    for entry in sorted(unit_dir.iterdir()):
        if not entry.is_file() or entry.suffix not in UNIT_SUFFIXES:
            continue
        try:
            content = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stack = quadlet.read_stack_marker(content)
        if stack is None:
            continue  # foreign file — never touched, never reported
        files.append(
            UnitFile(
                file_name=entry.name,
                stack=stack,
                service=quadlet.read_service_marker(content),
                spec_hash=quadlet.read_spec_hash(content),
            )
        )
    return tuple(files)


def write_env_files(tenant: str, files: dict[str, str]) -> None:
    """Write per-service EnvironmentFiles: 0600, tenant-owned. Paths come
    from quadlet.env_file_path and are re-checked to sit inside the tenant's
    stacks root."""
    validate_tenant_username(tenant)
    root = os.path.normpath(quadlet.stacks_root(tenant))
    for path, content in files.items():
        normalized = os.path.normpath(path)
        if normalized != root and not normalized.startswith(root + os.sep):
            raise StackHostError(f"Env file path escapes the stacks root: {path!r}")
        directory = os.path.dirname(normalized)
        os.makedirs(directory, exist_ok=True)
        _open_write(normalized, content, 0o600)
        shutil.chown(directory, user=tenant, group=tenant)
        shutil.chown(normalized, user=tenant, group=tenant)


def ensure_volume_dir(tenant: str, stack: str, volume: str) -> None:
    """Create one volume directory (and the stack tree above it), tenant-
    owned at every level the panel created."""
    validate_slug(stack, what="stack name")
    validate_slug(volume, what="volume name")
    target = quadlet.volume_host_dir(tenant, stack, volume)
    parts = [
        quadlet.stacks_root(tenant),
        quadlet.stack_dir(tenant, stack),
        f"{quadlet.stack_dir(tenant, stack)}/volumes",
        target,
    ]
    for path in parts:
        os.makedirs(path, exist_ok=True)
        shutil.chown(path, user=tenant, group=tenant)


async def remove_volume_dir(tenant: str, stack: str, volume: str) -> None:
    """Delete one volume directory and its data (rm -rf via the runner —
    volume trees can be large; never block the event loop)."""
    validate_slug(stack, what="stack name")
    validate_slug(volume, what="volume name")
    target = quadlet.volume_host_dir(tenant, stack, volume)
    result = await runner.run(["rm", "-rf", "--", target], timeout=600)
    if not result.ok:
        raise StackHostError(f"rm -rf {target} failed: {result.stderr.strip()[:400]}")


def scan_volume_dirs(tenant: str) -> dict[str, frozenset[str]]:
    """stack -> volume names found under the tenant's stacks root."""
    out: dict[str, frozenset[str]] = {}
    root = Path(quadlet.stacks_root(validate_tenant_username(tenant)))
    if not root.is_dir():
        return out
    for stack_entry in sorted(root.iterdir()):
        volumes_dir = stack_entry / "volumes"
        if not volumes_dir.is_dir():
            continue
        names = frozenset(v.name for v in volumes_dir.iterdir() if v.is_dir())
        if names:
            out[stack_entry.name] = names
    return out


def scan_tenant(uid: int, tenant: str) -> TenantScan:
    return TenantScan(unit_files=scan_units(uid), volume_dirs=scan_volume_dirs(tenant))
