"""Per-site-user systemd slices (Phase 11c): CPUQuota + MemoryMax.

Rendering is pure (snapshot-testable); installation writes the unit file and
daemon-reloads. Slices are derived from the site owner's effective limits —
no limits means no slice. Processes started for the site user (e.g. via
`systemd-run --slice=…`) are then resource-capped; PHP-FPM pool workers are
additionally bounded by the pool's pm.max_children + memory_limit, so the
slice acts as the outer cgroup backstop.
"""

from __future__ import annotations

import contextlib
import os

import structlog

from app.system import runner, systemd
from app.system.users import validate_site_username

log = structlog.get_logger("hosty.slices")

SLICE_DIR = "/etc/systemd/system"


def slice_name(site_user: str) -> str:
    validate_site_username(site_user)
    return f"hosty-{site_user}.slice"


def slice_path(site_user: str, *, slice_dir: str = SLICE_DIR) -> str:
    return f"{slice_dir}/{slice_name(site_user)}"


def render_slice(
    site_user: str, *, cpu_quota_percent: int | None, memory_max_mb: int | None
) -> str:
    """Pure: site user + limits in → exact slice unit out."""
    validate_site_username(site_user)
    lines = [
        "# Managed by HostyPanel — do not edit by hand.",
        "[Unit]",
        f"Description=HostyPanel resource slice for {site_user}",
        "",
        "[Slice]",
    ]
    if cpu_quota_percent is not None:
        if not 1 <= cpu_quota_percent <= 1600:
            raise ValueError(f"cpu_quota_percent out of range: {cpu_quota_percent}")
        lines.append(f"CPUQuota={cpu_quota_percent}%")
    if memory_max_mb is not None:
        if not 16 <= memory_max_mb <= 1048576:
            raise ValueError(f"memory_max_mb out of range: {memory_max_mb}")
        lines.append(f"MemoryMax={memory_max_mb}M")
    return "\n".join(lines) + "\n"


async def install_slice(
    site_user: str,
    *,
    cpu_quota_percent: int | None,
    memory_max_mb: int | None,
    slice_dir: str = SLICE_DIR,
) -> bool:
    """Write (or remove, when both limits are None) the slice unit. Idempotent.

    Returns True if a slice is now installed. Failures are logged, never
    raised: resource capping must not block site provisioning.
    """
    path = slice_path(site_user, slice_dir=slice_dir)
    try:
        if cpu_quota_percent is None and memory_max_mb is None:
            await remove_slice(site_user, slice_dir=slice_dir)  # unlimited: no slice
            return False
        content = render_slice(
            site_user, cpu_quota_percent=cpu_quota_percent, memory_max_mb=memory_max_mb
        )
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                if fh.read() == content:
                    return True  # already in the desired state
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        await runner.run(["systemctl", "daemon-reload"], timeout=30)
        log.info("slice_installed", slice=slice_name(site_user))
        return True
    except Exception as exc:
        log.warning("slice_install_failed", site_user=site_user, error=str(exc))
        return False


async def remove_slice(site_user: str, *, slice_dir: str = SLICE_DIR) -> bool:
    """Remove the slice unit. Idempotent: returns False if it wasn't there."""
    path = slice_path(site_user, slice_dir=slice_dir)
    if not os.path.exists(path):
        return False
    with contextlib.suppress(Exception):  # the slice may have no active cgroup — not an error
        await systemd.control("stop", slice_name(site_user))
    try:
        os.unlink(path)
        await runner.run(["systemctl", "daemon-reload"], timeout=30)
        log.info("slice_removed", slice=slice_name(site_user))
        return True
    except Exception as exc:
        log.warning("slice_remove_failed", site_user=site_user, error=str(exc))
        return False
