"""Server management — add/list/delete VPS hosts and validate SSH connectivity."""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.clock import utcnow
from app.core.errors import ConflictError, NotFoundError
from app.core.secrets import decrypt_secret
from app.db.models import Server, SshKey, User

router = APIRouter()

_SSH_TIMEOUT = 12  # seconds


# --- schemas -----------------------------------------------------------------------


class ServerCreate(BaseModel):
    name: str = Field(..., max_length=64, pattern=r"^[a-zA-Z0-9 _\-\.]+$")
    hostname: str = Field(..., max_length=255)
    port: int = Field(22, ge=1, le=65535)
    ssh_user: str = Field("root", max_length=64)
    ssh_key_id: int | None = None
    is_localhost: bool = False


class ServerResponse(BaseModel):
    id: int
    name: str
    hostname: str
    port: int
    ssh_user: str
    ssh_key_id: int | None
    is_localhost: bool
    status: str
    error_message: str | None
    os_info: str | None
    cpu_count: int | None
    memory_mb: int | None
    disk_free_gb: int | None
    podman_version: str | None
    created_at: datetime
    last_checked_at: datetime | None


def _to_response(s: Server) -> ServerResponse:
    return ServerResponse(
        id=s.id,
        name=s.name,
        hostname=s.hostname,
        port=s.port,
        ssh_user=s.ssh_user,
        ssh_key_id=s.ssh_key_id,
        is_localhost=s.is_localhost,
        status=s.status,
        error_message=s.error_message,
        os_info=s.os_info,
        cpu_count=s.cpu_count,
        memory_mb=s.memory_mb,
        disk_free_gb=s.disk_free_gb,
        podman_version=s.podman_version,
        created_at=s.created_at,
        last_checked_at=s.last_checked_at,
    )


# --- SSH validation ----------------------------------------------------------------


async def _probe_localhost() -> dict:
    """Probe the panel's own host without SSH."""
    info: dict = {}
    try:
        import psutil

        info["cpu_count"] = psutil.cpu_count(logical=True)
        mem = psutil.virtual_memory()
        info["memory_mb"] = mem.total // (1024 * 1024)
        disk = psutil.disk_usage("/")
        info["disk_free_gb"] = disk.free // (1024 ** 3)
    except Exception:
        pass

    # OS string
    try:
        import platform
        info["os_info"] = f"{platform.system()} {platform.release()} ({platform.machine()})"
    except Exception:
        pass

    # Podman version
    try:
        proc = await asyncio.create_subprocess_exec(
            "podman", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        line = stdout.decode().strip().splitlines()[0] if stdout else ""
        if line:
            info["podman_version"] = line.replace("podman version ", "").strip()[:64]
    except Exception:
        info["podman_version"] = None

    return info


async def _probe_remote(
    hostname: str,
    port: int,
    ssh_user: str,
    private_key_data: str,
) -> dict:
    """Open an SSH connection and gather host info."""
    # asyncssh is an optional runtime dep; import lazily so the module still
    # loads on systems where asyncssh is not yet installed.
    try:
        import asyncssh  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "asyncssh is not installed — run `uv sync` on the backend to add it"
        ) from exc

    try:
        private_key = asyncssh.import_private_key(private_key_data)
    except Exception as exc:
        raise RuntimeError(f"Could not parse SSH private key: {exc}") from exc

    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                hostname,
                port=port,
                username=ssh_user,
                client_keys=[private_key],
                known_hosts=None,  # first-connect; don't reject unknown hosts
            ),
            timeout=_SSH_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Connection timed out after {_SSH_TIMEOUT}s") from exc
    except Exception as exc:
        raise RuntimeError(f"SSH connection failed: {exc}") from exc

    info: dict = {}
    async with conn:
        async def _run(cmd: str) -> str:
            result = await conn.run(cmd, timeout=8)
            return (result.stdout or "").strip()

        # OS info
        try:
            pretty = await _run(
                r"grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '\"'"
            )
            if not pretty:
                pretty = await _run("uname -srm")
            info["os_info"] = pretty[:256] if pretty else None
        except Exception:
            pass

        # CPU
        try:
            nproc = await _run("nproc 2>/dev/null || grep -c processor /proc/cpuinfo")
            info["cpu_count"] = int(nproc) if nproc.isdigit() else None
        except Exception:
            pass

        # Memory (MB)
        try:
            mem_kb = await _run("grep MemTotal /proc/meminfo | awk '{print $2}'")
            info["memory_mb"] = int(mem_kb) // 1024 if mem_kb.isdigit() else None
        except Exception:
            pass

        # Disk free (GB) on /
        try:
            free_kb = await _run("df -k / | awk 'NR==2{print $4}'")
            info["disk_free_gb"] = int(free_kb) // (1024 * 1024) if free_kb.isdigit() else None
        except Exception:
            pass

        # Podman version
        try:
            pv = await _run("podman --version 2>/dev/null || echo ''")
            info["podman_version"] = pv.replace("podman version ", "").strip()[:64] if pv else None
        except Exception:
            pass

    return info


# --- CRUD routes -------------------------------------------------------------------


@router.get("", response_model=list[ServerResponse])
async def list_servers(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[ServerResponse]:
    rows = (await db.execute(select(Server).order_by(Server.id))).scalars().all()
    return [_to_response(s) for s in rows]


@router.post("", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(
    req: ServerCreate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ServerResponse:
    existing = (
        await db.execute(select(Server).where(Server.name == req.name))
    ).scalar_one_or_none()
    if existing:
        raise ConflictError("A server with that name already exists")

    if not req.is_localhost and req.ssh_key_id is None:
        raise ConflictError("Remote servers require an SSH key")

    if req.ssh_key_id is not None:
        key = await db.get(SshKey, req.ssh_key_id)
        if key is None:
            raise NotFoundError("SSH key not found")

    server = Server(
        name=req.name,
        hostname=req.hostname,
        port=req.port,
        ssh_user=req.ssh_user,
        ssh_key_id=req.ssh_key_id,
        is_localhost=req.is_localhost,
        status="pending",
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return _to_response(server)


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ServerResponse:
    server = await db.get(Server, server_id)
    if server is None:
        raise NotFoundError("Server not found")
    return _to_response(server)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> None:
    server = await db.get(Server, server_id)
    if server is None:
        raise NotFoundError("Server not found")
    if server.is_localhost:
        raise ConflictError("The localhost server cannot be deleted")
    await db.delete(server)
    await db.commit()


@router.post("/{server_id}/validate", response_model=ServerResponse)
async def validate_server(
    server_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ServerResponse:
    """SSH into the server (or probe localhost) and collect host information."""
    server = await db.get(Server, server_id)
    if server is None:
        raise NotFoundError("Server not found")

    settings = request.app.state.settings

    try:
        if server.is_localhost:
            info = await _probe_localhost()
        else:
            if server.ssh_key_id is None:
                raise RuntimeError("No SSH key assigned to this server")
            key_row = await db.get(SshKey, server.ssh_key_id)
            if key_row is None:
                raise RuntimeError("Assigned SSH key no longer exists")
            private_key_data = decrypt_secret(key_row.private_key_encrypted, settings.secret_key)
            info = await _probe_remote(
                hostname=server.hostname,
                port=server.port,
                ssh_user=server.ssh_user,
                private_key_data=private_key_data,
            )

        server.status = "connected"
        server.error_message = None
        server.os_info = info.get("os_info")
        server.cpu_count = info.get("cpu_count")
        server.memory_mb = info.get("memory_mb")
        server.disk_free_gb = info.get("disk_free_gb")
        server.podman_version = info.get("podman_version")

    except Exception as exc:
        server.status = "error"
        server.error_message = str(exc)[:500]

    server.last_checked_at = utcnow()
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return _to_response(server)
