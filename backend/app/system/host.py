"""HostContext — route command execution and file I/O to localhost or a
remote server over SSH/SFTP (v2 multi-server, ADR-013).

Each StackSpec carries a `server_id`; the reconciler builds the appropriate
HostContext before executing a plan.  LocalHost delegates to the existing
local functions so the executor + tests are unchanged.  RemoteSSHHost uses
asyncssh to run commands and SFTP to manage files.
"""

from __future__ import annotations

import asyncio
import io
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from app.system import runner

# fmt: off
__all__ = ["HostContext", "LocalHost", "RemoteSSHHost", "CommandResult"]
# fmt: on

CommandResult = runner.CommandResult


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class HostContext(ABC):
    """Capability surface the executor and observer need from a host."""

    @property
    def is_localhost(self) -> bool:
        return False

    @abstractmethod
    async def run(self, argv: list[str], *, timeout: float = 120) -> CommandResult: ...

    # --- stackhost-level operations ------------------------------------------

    @abstractmethod
    async def sync_units(self, uid: int, stack: str, tenant: str, desired: dict[str, str]) -> bool: ...

    @abstractmethod
    async def remove_units(self, uid: int, stack: str, tenant: str) -> bool: ...

    @abstractmethod
    async def scan_tenant(self, uid: int, tenant: str): ...  # → TenantScan

    @abstractmethod
    async def sync_env_files(self, tenant: str, stack: str, files: dict[str, str]) -> bool: ...

    @abstractmethod
    async def remove_env_files(self, tenant: str, stack: str) -> bool: ...

    @abstractmethod
    async def sync_ssh_keys(self, tenant: str, keys: list[tuple[str, str]]) -> None: ...

    @abstractmethod
    async def ensure_volume_dir(self, tenant: str, stack: str, volume: str) -> None: ...

    @abstractmethod
    async def remove_volume_dir(self, tenant: str, stack: str, volume: str) -> None: ...

    # --- systemd_user operations ----------------------------------------------

    @abstractmethod
    async def daemon_reload(self, user: str) -> None: ...

    @abstractmethod
    async def control_service(self, user: str, action: str, unit: str) -> None: ...

    # --- podman operations ----------------------------------------------------

    @abstractmethod
    async def podman_ps(self, uid: int) -> list: ...

    @abstractmethod
    async def remove_stack_containers(self, uid: int, stack: str) -> None: ...

    # --- tenant operations ---------------------------------------------------

    @abstractmethod
    async def tenant_exists(self, name: str) -> bool: ...

    @abstractmethod
    async def provision_tenant(self, name: str, *, subuid_start: int, subuid_count: int): ...

    @abstractmethod
    async def remove_tenant(self, name: str) -> bool: ...

    # --- observer helpers ----------------------------------------------------

    @abstractmethod
    async def user_manager_ready(self, uid: int) -> bool: ...


# ---------------------------------------------------------------------------
# LocalHost — delegates to existing local functions
# ---------------------------------------------------------------------------


class LocalHost(HostContext):
    """Localhost: wraps the existing system/* functions in async thread calls
    where needed. The test suite patches those functions at module level so
    LocalHost keeps every existing mock working unchanged."""

    @property
    def is_localhost(self) -> bool:
        return True

    async def run(self, argv: list[str], *, timeout: float = 120) -> CommandResult:
        return await runner.run(argv, timeout=timeout)

    async def sync_units(self, uid: int, stack: str, tenant: str, desired: dict[str, str]) -> bool:
        from app.system import stackhost
        return await asyncio.to_thread(stackhost.sync_units, uid, stack, tenant, desired)

    async def remove_units(self, uid: int, stack: str, tenant: str) -> bool:
        from app.system import stackhost
        return await asyncio.to_thread(stackhost.remove_units, uid, stack, tenant)

    async def scan_tenant(self, uid: int, tenant: str):
        from app.system import stackhost
        return await asyncio.to_thread(stackhost.scan_tenant, uid, tenant)

    async def sync_env_files(self, tenant: str, stack: str, files: dict[str, str]) -> bool:
        from app.system import stackhost
        return await asyncio.to_thread(stackhost.sync_env_files, tenant, stack, files)

    async def remove_env_files(self, tenant: str, stack: str) -> bool:
        from app.system import stackhost
        return await asyncio.to_thread(stackhost.remove_env_files, tenant, stack)

    async def sync_ssh_keys(self, tenant: str, keys: list[tuple[str, str]]) -> None:
        from app.system import stackhost
        await asyncio.to_thread(stackhost.sync_ssh_keys, tenant, keys)

    async def ensure_volume_dir(self, tenant: str, stack: str, volume: str) -> None:
        from app.system import stackhost
        await asyncio.to_thread(stackhost.ensure_volume_dir, tenant, stack, volume)

    async def remove_volume_dir(self, tenant: str, stack: str, volume: str) -> None:
        from app.system import stackhost
        await stackhost.remove_volume_dir(tenant, stack, volume)

    async def daemon_reload(self, user: str) -> None:
        from app.system import systemd_user
        await systemd_user.daemon_reload(user)

    async def control_service(self, user: str, action: str, unit: str) -> None:
        from app.system import systemd_user
        await systemd_user.control(user, action, unit)

    async def podman_ps(self, uid: int) -> list:
        from app.system import podman
        return await podman.ps(uid)

    async def remove_stack_containers(self, uid: int, stack: str) -> None:
        from app.system import podman
        await podman.remove_stack_containers(uid, stack)

    async def tenant_exists(self, name: str) -> bool:
        from app.system import tenants as tenants_sys
        return await tenants_sys.exists(name)

    async def provision_tenant(self, name: str, *, subuid_start: int, subuid_count: int):
        from app.system import tenants as tenants_sys
        return await tenants_sys.provision(name, subuid_start=subuid_start, subuid_count=subuid_count)

    async def remove_tenant(self, name: str) -> bool:
        from app.system import tenants as tenants_sys
        return await tenants_sys.remove(name)

    async def user_manager_ready(self, uid: int) -> bool:
        return Path(f"/run/user/{uid}/bus").is_socket()


# ---------------------------------------------------------------------------
# RemoteSSHHost — asyncssh-based
# ---------------------------------------------------------------------------


class _SSHRunError(RuntimeError):
    pass


@dataclass
class RemoteSSHHost(HostContext):
    """Remote server over asyncssh. Call `connect()` before use; call
    `close()` when done (or use as an async context manager)."""

    hostname: str
    port: int
    username: str
    private_key_text: str
    _conn: object = field(default=None, repr=False, compare=False)
    _sftp: object = field(default=None, repr=False, compare=False)

    # --- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        try:
            import asyncssh  # lazy: not available in all test environments
        except ImportError as exc:
            raise RuntimeError("asyncssh is required for remote hosts") from exc
        key = asyncssh.import_private_key(self.private_key_text)
        self._conn = await asyncssh.connect(
            self.hostname,
            port=self.port,
            username=self.username,
            client_keys=[key],
            known_hosts=None,
        )
        try:
            self._sftp = await self._conn.start_sftp_client()
        except Exception:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None
            raise

    async def close(self) -> None:
        if self._sftp is not None:
            self._sftp.exit()
            self._sftp = None
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    async def __aenter__(self) -> "RemoteSSHHost":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # --- internal helpers ----------------------------------------------------

    async def _ssh_run(
        self, argv: list[str], *, timeout: float = 120, ok_codes: set[int] | None = None
    ) -> CommandResult:
        import shlex
        cmd = " ".join(shlex.quote(a) for a in argv)
        result = await asyncio.wait_for(self._conn.run(cmd), timeout=timeout)  # type: ignore[union-attr]
        rc = result.exit_status if result.exit_status is not None else -1
        ok = rc == 0 if ok_codes is None else rc in ok_codes
        cr = CommandResult(
            argv=tuple(argv),
            returncode=rc,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            duration_ms=0,
        )
        if not ok:
            raise _SSHRunError(
                f"{argv[0]} failed (exit {rc}): {cr.stderr.strip()[:400] or cr.stdout.strip()[:400]}"
            )
        return cr

    async def _sftp_makedirs(self, path: str) -> None:
        try:
            await self._sftp.stat(path)  # type: ignore[union-attr]
        except Exception:
            await self._ssh_run(["mkdir", "-p", "--", path])

    async def _sftp_read(self, path: str) -> str:
        buf = io.BytesIO()
        await self._sftp.getfo(path, buf)  # type: ignore[union-attr]
        return buf.getvalue().decode(errors="replace")

    async def _sftp_write(self, path: str, content: str, mode: int = 0o644) -> None:
        await self._sftp.putfo(io.BytesIO(content.encode()), path)  # type: ignore[union-attr]
        await self._sftp.chmod(path, mode)  # type: ignore[union-attr]

    async def _sftp_listdir(self, path: str) -> list[str]:
        entries = await self._sftp.listdir(path)  # type: ignore[union-attr]
        return [e if isinstance(e, str) else e.filename for e in entries]

    # --- HostContext implementation -------------------------------------------

    async def run(self, argv: list[str], *, timeout: float = 120) -> CommandResult:
        return await self._ssh_run(argv, timeout=timeout)

    async def _sync_remote_dir(
        self, dir_path: str, stack: str, desired: dict[str, str]
    ) -> bool:
        from app.system import quadlet, stackhost
        await self._sftp_makedirs(dir_path)
        existing: dict[str, str] = {}
        try:
            for entry in await self._sftp_listdir(dir_path):
                if not any(entry.endswith(s) for s in stackhost.UNIT_SUFFIXES):
                    continue
                try:
                    content = await self._sftp_read(f"{dir_path}/{entry}")
                    if quadlet.read_stack_marker(content) == stack:
                        existing[entry] = content
                except Exception:
                    continue
        except Exception:
            pass
        changed = False
        for fname, content in desired.items():
            if existing.get(fname) != content:
                await self._sftp_write(f"{dir_path}/{fname}", content, 0o644)
                changed = True
        for fname in existing:
            if fname not in desired:
                await self._sftp.remove(f"{dir_path}/{fname}")  # type: ignore[union-attr]
                changed = True
        return changed

    async def sync_units(self, uid: int, stack: str, tenant: str, desired: dict[str, str]) -> bool:
        from app.system import quadlet, stackhost
        quadlet_files = {f: c for f, c in desired.items() if any(f.endswith(s) for s in stackhost._QUADLET_SUFFIXES)}
        service_files = {f: c for f, c in desired.items() if not any(f.endswith(s) for s in stackhost._QUADLET_SUFFIXES)}

        changed = await self._sync_remote_dir(quadlet.unit_dir(uid), stack, quadlet_files)

        svc_dir = quadlet.systemd_user_unit_dir(tenant)
        has_existing = False
        try:
            for entry in await self._sftp_listdir(svc_dir):
                try:
                    content = await self._sftp_read(f"{svc_dir}/{entry}")
                    if quadlet.read_stack_marker(content) == stack:
                        has_existing = True
                        break
                except Exception:
                    pass
        except Exception:
            pass

        if service_files or has_existing:
            changed |= await self._sync_remote_dir(svc_dir, stack, service_files)
            if service_files:
                # Chown ALL intermediate dirs (.config/, .config/systemd/, .config/systemd/user/)
                import posixpath
                for d in [posixpath.dirname(posixpath.dirname(svc_dir)),
                          posixpath.dirname(svc_dir), svc_dir]:
                    await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", d],
                                        ok_codes={0, 1})
                for fname in service_files:
                    await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", f"{svc_dir}/{fname}"])

        # For git blueprint stacks, ensure the workspace parent dir exists and is
        # tenant-owned even when there are no env files.
        if any(f.endswith("-git-sync.service") for f in service_files):
            from app.system import quadlet as _q
            stacks_dir = _q.stacks_root(tenant)
            stack_dir = _q.stack_dir(tenant, stack)
            for d in [stacks_dir, stack_dir]:
                await self._sftp_makedirs(d)
                await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", d])

        return changed

    async def remove_units(self, uid: int, stack: str, tenant: str) -> bool:
        return await self.sync_units(uid, stack, tenant, {})

    async def _scan_remote_unit_dir(self, dir_path: str) -> "list":
        from app.system import quadlet, stackhost
        from app.system.stackhost import UnitFile
        files: list[UnitFile] = []
        try:
            for entry in sorted(await self._sftp_listdir(dir_path)):
                if not any(entry.endswith(s) for s in stackhost.UNIT_SUFFIXES):
                    continue
                try:
                    content = await self._sftp_read(f"{dir_path}/{entry}")
                    s = quadlet.read_stack_marker(content)
                    if s is None:
                        continue
                    files.append(UnitFile(
                        file_name=entry,
                        stack=s,
                        service=quadlet.read_service_marker(content),
                        spec_hash=quadlet.read_spec_hash(content),
                    ))
                except Exception:
                    continue
        except Exception:
            pass
        return files

    async def scan_tenant(self, uid: int, tenant: str):
        from app.system import quadlet, stackhost
        from app.system.stackhost import TenantScan
        unit_files = await self._scan_remote_unit_dir(quadlet.unit_dir(uid))
        unit_files += await self._scan_remote_unit_dir(quadlet.systemd_user_unit_dir(tenant))
        volume_dirs: dict[str, frozenset[str]] = {}
        stacks_root = quadlet.stacks_root(tenant)
        try:
            for stack_name in await self._sftp_listdir(stacks_root):
                volumes_path = f"{stacks_root}/{stack_name}/volumes"
                try:
                    vols = frozenset(await self._sftp_listdir(volumes_path))
                    if vols:
                        volume_dirs[stack_name] = vols
                except Exception:
                    pass
        except Exception:
            pass
        return TenantScan(unit_files=tuple(unit_files), volume_dirs=volume_dirs)

    async def sync_env_files(self, tenant: str, stack: str, files: dict[str, str]) -> bool:
        from app.system import quadlet
        env_dir = f"{quadlet.stack_dir(tenant, stack)}/env"
        existing: dict[str, str] = {}
        try:
            for entry in await self._sftp_listdir(env_dir):
                path = f"{env_dir}/{entry}"
                try:
                    existing[path] = await self._sftp_read(path)
                except Exception:
                    continue
        except Exception:
            pass
        if files:
            await self._sftp_makedirs(env_dir)
            from app.system import quadlet as _q
            for d in [_q.stacks_root(tenant), _q.stack_dir(tenant, stack), env_dir]:
                await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", d],
                                    ok_codes={0, 1})
        changed = False
        for path, content in files.items():
            if existing.get(path) != content:
                await self._sftp_write(path, content, 0o600)
                await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", path])
                changed = True
        for path in existing:
            if path not in files:
                await self._ssh_run(["rm", "-f", "--", path])
                changed = True
        if not files:
            try:
                remaining = await self._sftp_listdir(env_dir)
                if not remaining:
                    await self._ssh_run(["rmdir", "--", env_dir], ok_codes={0, 1})
            except Exception:
                pass
        return changed

    async def remove_env_files(self, tenant: str, stack: str) -> bool:
        return await self.sync_env_files(tenant, stack, {})

    async def sync_ssh_keys(self, tenant: str, keys: list[tuple[str, str]]) -> None:
        from app.system import quadlet
        home = quadlet.home_dir_for(tenant)
        ssh_dir = f"{home}/.ssh"
        await self._ssh_run(["mkdir", "-p", "--", ssh_dir])
        await self._ssh_run(["chmod", "700", "--", ssh_dir])
        await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", ssh_dir])
        key_paths = []
        for name, content in keys:
            path = f"{ssh_dir}/{name}"
            await self._sftp_write(path, content, 0o600)
            await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", path])
            key_paths.append(path)
        allowed = {k[0] for k in keys}
        try:
            for entry in await self._sftp_listdir(ssh_dir):
                if entry not in ("config", "known_hosts") and entry not in allowed:
                    await self._ssh_run(["rm", "-f", "--", f"{ssh_dir}/{entry}"])
        except Exception:
            pass
        config_lines = [
            "Host github.com gitlab.com bitbucket.org",
            "  StrictHostKeyChecking accept-new",
        ]
        for kp in key_paths:
            config_lines.append(f"  IdentityFile {kp}")
        config_lines.append("  IdentitiesOnly yes")
        config_content = "\n".join(config_lines) + "\n"
        await self._sftp_write(f"{ssh_dir}/config", config_content, 0o600)
        await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", f"{ssh_dir}/config"])

    async def ensure_volume_dir(self, tenant: str, stack: str, volume: str) -> None:
        from app.system import quadlet
        target = quadlet.volume_host_dir(tenant, stack, volume)
        for path in [
            quadlet.stacks_root(tenant),
            quadlet.stack_dir(tenant, stack),
            f"{quadlet.stack_dir(tenant, stack)}/volumes",
            target,
        ]:
            await self._ssh_run(["mkdir", "-p", "--", path])
            await self._ssh_run(["chown", f"{tenant}:{tenant}", "--", path])

    async def remove_volume_dir(self, tenant: str, stack: str, volume: str) -> None:
        from app.system import quadlet
        target = quadlet.volume_host_dir(tenant, stack, volume)
        await self._ssh_run(["rm", "-rf", "--", target], timeout=600)

    async def daemon_reload(self, user: str) -> None:
        from app.system import systemd_user
        from app.system.systemd_user import SystemdUserError
        try:
            await self._ssh_run(systemd_user.build_daemon_reload_argv(user))
        except _SSHRunError as exc:
            raise SystemdUserError(str(exc)) from exc

    async def control_service(self, user: str, action: str, unit: str) -> None:
        from app.system import systemd_user
        from app.system.systemd_user import SystemdUserError
        try:
            await self._ssh_run(systemd_user.build_control_argv(user, action, unit))
        except _SSHRunError as exc:
            raise SystemdUserError(str(exc)) from exc

    async def podman_ps(self, uid: int) -> list:
        from app.system import podman
        try:
            result = await self._ssh_run(podman.build_ps_argv(uid))
            return podman.parse_ps(result.stdout)
        except (_SSHRunError, Exception):
            return []

    async def remove_stack_containers(self, uid: int, stack: str) -> None:
        from app.system import podman
        try:
            listed = await self._ssh_run(podman.build_stack_ps_ids_argv(uid, stack))
        except _SSHRunError:
            return
        ids = [cid for cid in listed.stdout.split() if cid]
        if not ids:
            return
        base = ["podman", "--url", podman.socket_url(uid)]
        try:
            await self._ssh_run([*base, "rm", "--force", "--", *ids], timeout=180)
        except _SSHRunError:
            pass

    async def tenant_exists(self, name: str) -> bool:
        from app.system import tenants as tenants_sys
        tenants_sys.validate_tenant_username(name)
        try:
            result = await self._ssh_run(tenants_sys.build_uid_argv(name), ok_codes={0, 1})
            return result.returncode == 0
        except Exception:
            return False

    async def provision_tenant(self, name: str, *, subuid_start: int, subuid_count: int):
        from app.system import tenants as tenants_sys
        from app.system.tenants import TenantInfo, TenantOperationError
        if not await self.tenant_exists(name):
            await self._ssh_run(tenants_sys.build_useradd_argv(name))
        await self._ssh_run(tenants_sys.build_add_subuids_argv(name, subuid_start, subuid_count))
        await self._ssh_run(tenants_sys.build_add_subgids_argv(name, subuid_start, subuid_count))
        await self._ssh_run(tenants_sys.build_linger_argv(name, enable=True))
        uid_result = await self._ssh_run(tenants_sys.build_uid_argv(name))
        try:
            uid = int(uid_result.stdout.strip())
        except ValueError as exc:
            raise TenantOperationError(f"Unparseable remote uid for {name}") from exc
        for _ in range(40):
            if await self.user_manager_ready(uid):
                break
            await asyncio.sleep(0.25)
        else:
            raise TenantOperationError(f"Remote user manager did not become ready for uid {uid}")
        return TenantInfo(
            linux_user=name, uid=uid, subuid_start=subuid_start, subuid_count=subuid_count
        )

    async def remove_tenant(self, name: str) -> bool:
        from app.system import tenants as tenants_sys
        if not await self.tenant_exists(name):
            return False
        await self._ssh_run(tenants_sys.build_linger_argv(name, enable=False))
        await self._ssh_run(tenants_sys.build_userdel_argv(name), timeout=60)
        return True

    async def user_manager_ready(self, uid: int) -> bool:
        try:
            result = await self._ssh_run(
                ["test", "-S", f"/run/user/{uid}/bus"], ok_codes={0, 1}
            )
            return result.returncode == 0
        except Exception:
            return False
