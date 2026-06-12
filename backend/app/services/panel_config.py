"""Panel domain & HTTPS configuration, set from the UI.

Changing the panel domain does three things atomically from the user's view:
the runtime Settings object is updated (takes effect on the next request),
Caddy is re-synced so it serves the panel vhost and obtains a certificate,
and the change is persisted to the env file so it survives restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
import tempfile

import structlog

log = structlog.get_logger("hosty.panel_config")

_LINE_RE = re.compile(r"^([A-Z0-9_]+)=")


def update_env_file(path: str, updates: dict[str, str | None]) -> None:
    """Set (or remove, when value is None) KEY=VALUE lines, preserving the rest.

    Written atomically with the original file mode (0600 for fresh files).
    """
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        mode = os.stat(path).st_mode & 0o777
    else:
        mode = 0o600

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        m = _LINE_RE.match(line)
        key = m.group(1) if m else None
        if key is not None and key in remaining:
            value = remaining.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
            # None: drop the line entirely.
        else:
            out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={value}")

    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".hosty-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    log.info("env_file_updated", path=path, keys=sorted(updates))


async def resolve_ips(domain: str) -> list[str]:
    """All A/AAAA addresses the domain currently resolves to ([] when none)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})
