"""Tracked backup and restore operations for v2 stacks."""

from __future__ import annotations

import json
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Operation, Stack
from app.orchestration.blueprints import get_blueprint
from app.orchestration.operations import finish, set_step
from app.services import backup, s3_config
from app.services.stacks import decrypt_inputs, stack_children, tenant_for
from app.system import quadlet, systemd_user

log = structlog.get_logger("hosty.stack_backup_ops")

Step = tuple[str, str]
Action = Callable[[], Awaitable[None]]

BACKUP_STEPS: list[Step] = [
    ("prepare", "Prepare staging directory"),
    ("blueprint", "Prepare blueprint backup data"),
    ("stop", "Stop stack services for a consistent volume snapshot"),
    ("volumes", "Archive stack volumes"),
    ("start", "Restart stack services"),
    ("finalize", "Write manifest and apply retention"),
]

RESTORE_STEPS: list[Step] = [
    ("fetch", "Fetch backup (from S3 if not local)"),
    ("verify", "Verify checksums"),
    ("stop", "Stop stack services"),
    ("volumes", "Restore stack volumes"),
    ("start", "Start stack services"),
    ("blueprint", "Restore blueprint data"),
]


def backup_key(stack_name: str) -> str:
    return f"stack--{stack_name}"


def initial_steps(steps: list[Step]) -> str:
    return json.dumps(
        [{"name": name, "label": label, "status": "pending"} for name, label in steps]
    )


async def _run_step(db: AsyncSession, op: Operation, name: str, action: Action) -> None:
    await set_step(db, op, name, "running")
    try:
        await action()
    except Exception:
        await set_step(db, op, name, "failed")
        raise
    await set_step(db, op, name, "done")


async def _resolve_s3(
    db: AsyncSession, settings: Settings, override: backup.S3Like | None
) -> tuple[backup.S3Like | None, str]:
    config = await s3_config.load(db, settings)
    prefix = config.prefix if config is not None else settings.s3_prefix
    if override is not None:
        return override, prefix
    if config is None:
        return None, prefix
    return (
        backup.S3Client(
            endpoint=config.endpoint,
            bucket=config.bucket,
            access_key=config.access_key,
            secret_key=config.secret_key,
            region=config.region,
        ),
        prefix,
    )


def _volume_paths(stack: Stack) -> tuple[str, str]:
    if stack.owner_id is None:
        raise backup.BackupError("Stack has no owner")
    tenant = tenant_for(stack.owner_id)
    stack_root = f"/home/{tenant}/stacks/{stack.name}"
    return stack_root, f"{stack_root}/volumes"


async def run_backup_stack(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    stack_id: int,
    operation_id: int,
    s3: backup.S3Like | None = None,
) -> None:
    async with backup.operation_lock, sessionmaker() as db:
        stack = await db.get(Stack, stack_id)
        op = await db.get(Operation, operation_id)
        assert stack is not None and op is not None
        op.status = "running"
        await db.commit()

        blueprint = get_blueprint(stack.blueprint_id)
        if blueprint is None:
            await finish(db, op, status="failed", error="Blueprint is no longer registered")
            return
        hooks = blueprint.backup_hooks()
        inputs = decrypt_inputs(stack, settings)
        backup_id = backup.new_backup_id()
        key = backup_key(stack.name)
        workload_dir = Path(settings.backups_root) / key
        staging = workload_dir / f".staging-{backup_id}"
        final = workload_dir / backup_id
        stack_root, volumes_dir = _volume_paths(stack)
        s3_client, prefix = await _resolve_s3(db, settings, s3)
        services, _, _ = await stack_children(db, stack.id)
        tenant = tenant_for(stack.owner_id)
        ordered = sorted(services, key=lambda service: (service.name != "db", service.name))

        async def prepare() -> None:
            staging.mkdir(parents=True, exist_ok=True)

        async def blueprint_data() -> None:
            if hooks is not None:
                await hooks.pre_backup(db=db, settings=settings, stack=stack, directory=staging)

        async def stop() -> None:
            for service in reversed(ordered):
                await systemd_user.control(
                    tenant, "stop", quadlet.service_unit_name(stack.name, service.name)
                )

        async def volumes() -> None:
            source = backup.validate_managed_directory(volumes_dir, allowed_root=stack_root)
            await backup.archive_files(source, staging)

        async def start() -> None:
            for service in ordered:
                await systemd_user.control(
                    tenant, "start", quadlet.service_unit_name(stack.name, service.name)
                )

        async def finalize_backup() -> None:
            backup.write_manifest(
                staging,
                backup_id=backup_id,
                domain=key,
                php_version=str(inputs.get("php_version", "")),
                wordpress=stack.blueprint_id == "wordpress",
                databases=list(hooks.databases if hooks is not None else ()),
                workload_kind="stack",
                workload_name=stack.name,
                blueprint_id=stack.blueprint_id,
            )
            staging.rename(final)
            backup.prune(settings.backups_root, key, 7)

        try:
            await _run_step(db, op, "prepare", prepare)
            await _run_step(db, op, "blueprint", blueprint_data)
            await _run_step(db, op, "stop", stop)
            await _run_step(db, op, "volumes", volumes)
            await _run_step(db, op, "start", start)
            await _run_step(db, op, "finalize", finalize_backup)
            if s3_client is not None:
                await backup.upload_backup(s3_client, prefix, final, key, backup_id)
            await finish(db, op, status="succeeded")
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            for service in ordered:
                with suppress(Exception):
                    await systemd_user.control(
                        tenant, "start", quadlet.service_unit_name(stack.name, service.name)
                    )
            await finish(db, op, status="failed", error=str(exc)[:500])
            log.error("stack_backup_failed", stack=stack.name, error=str(exc))


async def run_restore_stack(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    stack_id: int,
    operation_id: int,
    backup_id: str,
    scope: str,
    s3: backup.S3Like | None = None,
) -> None:
    async with backup.operation_lock, sessionmaker() as db:
        stack = await db.get(Stack, stack_id)
        op = await db.get(Operation, operation_id)
        assert stack is not None and op is not None
        op.status = "running"
        await db.commit()

        blueprint = get_blueprint(stack.blueprint_id)
        if blueprint is None:
            await finish(db, op, status="failed", error="Blueprint is no longer registered")
            return
        hooks = blueprint.backup_hooks()
        key = backup_key(stack.name)
        directory = backup.backup_dir(settings.backups_root, key, backup_id)
        s3_client, prefix = await _resolve_s3(db, settings, s3)
        stack_root, volumes_dir = _volume_paths(stack)
        services, _, _ = await stack_children(db, stack.id)
        tenant = tenant_for(stack.owner_id)
        ordered = sorted(services, key=lambda service: (service.name != "db", service.name))

        async def fetch() -> None:
            if backup.read_manifest(directory) is not None:
                return
            if s3_client is None:
                raise backup.BackupError(f"Backup {backup_id} is not available locally")
            await backup.download_backup(s3_client, prefix, settings.backups_root, key, backup_id)

        async def verify() -> None:
            manifest = backup.read_manifest(directory)
            if manifest is None:
                raise backup.BackupError(f"Backup {backup_id} has no readable manifest")
            if (
                manifest.get("workload_kind") != "stack"
                or manifest.get("workload_name") != stack.name
            ):
                raise backup.BackupError("Backup belongs to a different stack")
            backup.verify_checksums(directory, manifest)

        async def stop() -> None:
            for service in reversed(ordered):
                await systemd_user.control(
                    tenant, "stop", quadlet.service_unit_name(stack.name, service.name)
                )

        async def volumes() -> None:
            if scope in ("full", "files"):
                await backup.restore_tree(
                    directory,
                    volumes_dir,
                    allowed_root=stack_root,
                    staging_root=settings.restore_staging_root,
                    owner=tenant,
                )

        async def start() -> None:
            for service in ordered:
                await systemd_user.control(
                    tenant, "start", quadlet.service_unit_name(stack.name, service.name)
                )

        async def blueprint_data() -> None:
            if scope in ("full", "db") and hooks is not None:
                await hooks.post_restore(db=db, settings=settings, stack=stack, directory=directory)

        try:
            await _run_step(db, op, "fetch", fetch)
            await _run_step(db, op, "verify", verify)
            await _run_step(db, op, "stop", stop)
            await _run_step(db, op, "volumes", volumes)
            await _run_step(db, op, "start", start)
            await _run_step(db, op, "blueprint", blueprint_data)
            await finish(db, op, status="succeeded")
        except Exception as exc:
            # Once stop completed, availability is more important than
            # preserving the failed restore's intermediate state.
            for service in ordered:
                with suppress(Exception):
                    await systemd_user.control(
                        tenant, "start", quadlet.service_unit_name(stack.name, service.name)
                    )
            await finish(db, op, status="failed", error=str(exc)[:500])
            log.error("stack_restore_failed", stack=stack.name, error=str(exc))
