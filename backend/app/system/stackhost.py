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

UNIT_SUFFIXES = (".build", ".container", ".network")


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


def sync_env_files(tenant: str, stack: str, files: dict[str, str]) -> bool:
    """Make the stack env directory hold exactly ``files``.

    Environment files contain credentials, so stale files are deleted on
    every rewrite instead of being left behind after service/env changes.
    """
    validate_tenant_username(tenant)
    validate_slug(stack, what="stack name")
    env_dir = os.path.normpath(os.path.join(quadlet.stack_dir(tenant, stack), "env"))
    desired: dict[str, str] = {}
    for path, content in files.items():
        normalized = os.path.normpath(path)
        if os.path.dirname(normalized) != env_dir:
            raise StackHostError(f"Env file path escapes the stack env directory: {path!r}")
        desired[normalized] = content

    existing: dict[str, str] = {}
    if os.path.isdir(env_dir):
        for entry in Path(env_dir).iterdir():
            if entry.is_file():
                try:
                    existing[str(entry)] = entry.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

    changed = False
    if desired:
        os.makedirs(env_dir, exist_ok=True)
        shutil.chown(env_dir, user=tenant, group=tenant)
    for path, content in desired.items():
        if existing.get(path) != content:
            _open_write(path, content, 0o600)
            changed = True
        else:
            os.chmod(path, 0o600)
        shutil.chown(path, user=tenant, group=tenant)
    for path in existing:
        if path not in desired:
            os.unlink(path)
            changed = True
    if os.path.isdir(env_dir) and not os.listdir(env_dir):
        os.rmdir(env_dir)
    return changed


def write_env_files(tenant: str, files: dict[str, str]) -> None:
    """Compatibility wrapper for callers that only write one stack."""
    if not files:
        return
    first = Path(next(iter(files)))
    sync_env_files(tenant, first.parent.parent.name, files)


def remove_env_files(tenant: str, stack: str) -> bool:
    return sync_env_files(tenant, stack, {})


def sync_ssh_keys(tenant: str, keys: list[tuple[str, str]]) -> None:
    """Write SSH private keys to the tenant's ~/.ssh directory and generate an
    SSH config file that uses them for all connections. Keys is a list of
    (name, private_key_text)."""
    validate_tenant_username(tenant)
    home = quadlet.home_dir_for(tenant)
    ssh_dir = os.path.join(home, ".ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    shutil.chown(ssh_dir, user=tenant, group=tenant)

    # Write each private key
    key_paths = []
    for name, content in keys:
        validate_slug(name, what="ssh key name")
        path = os.path.join(ssh_dir, name)
        _open_write(path, content, 0o600)
        shutil.chown(path, user=tenant, group=tenant)
        key_paths.append(path)

    # Clean up old keys (anything not in the current set, ignoring known_hosts/config)
    allowed = {k[0] for k in keys}
    for entry in os.listdir(ssh_dir):
        if entry not in ("config", "known_hosts") and entry not in allowed:
            os.remove(os.path.join(ssh_dir, entry))

    # Generate config to blanket-apply all keys to common git hosts
    config_lines = [
        "Host github.com gitlab.com bitbucket.org",
        "  StrictHostKeyChecking accept-new",
    ]
    for kp in key_paths:
        config_lines.append(f"  IdentityFile {kp}")
    config_lines.append("  IdentitiesOnly yes")

    config_path = os.path.join(ssh_dir, "config")
    _open_write(config_path, "\n".join(config_lines) + "\n", 0o600)
    shutil.chown(config_path, user=tenant, group=tenant)


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
