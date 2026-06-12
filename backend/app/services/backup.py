"""Site backups (Phase 8).

On-disk layout (`<backups_root>/<domain>/<backup_id>/`):

    files.tar.zst    archive of the whole site directory
    db/<name>.sql    one mysqldump per panel-managed database
    manifest.json    metadata + sha256 checksums — written LAST, so a
                     directory without a valid manifest is incomplete
                     and is ignored (and eventually pruned)

Optional S3 mirror under `<s3_prefix>/<domain>/<backup_id>/...` (MinIO-
compatible). The panel never deletes S3 objects; local retention only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import structlog

from app import __version__
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.services import mariadb
from app.system import fs, runner

log = structlog.get_logger("hosty.backup")

MANIFEST_VERSION = 1
FILES_ARCHIVE = "files.tar.zst"
MANIFEST = "manifest.json"
DB_DIR = "db"

BACKUP_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
FREQUENCIES = ("daily", "weekly")

# One backup/restore at a time: these are I/O-heavy root operations.
operation_lock = asyncio.Lock()


class BackupError(AppError):
    status_code = 502
    code = "backup_error"


# --- ids and paths -----------------------------------------------------------------


def new_backup_id(now: datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")


def validate_backup_id(raw: str) -> str:
    """Strict format check — also makes path traversal structurally impossible."""
    if not isinstance(raw, str) or not BACKUP_ID_RE.fullmatch(raw):
        raise NotFoundError(f"Unknown backup id: {raw!r}")
    return raw


def backup_dir(root: str, domain: str, backup_id: str) -> Path:
    return Path(root) / domain / validate_backup_id(backup_id)


# --- command builders (exact-argv tested) -------------------------------------------


def build_tar_create_argv(archive: Path, site_dir: str) -> list[str]:
    return ["tar", "--zstd", "-cf", str(archive), "-C", site_dir, "."]


def build_tar_extract_argv(staging_dir: str) -> list[str]:
    return [
        "systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--uid=hosty-restore",
        "--property=NoNewPrivileges=yes",
        "--property=PrivateDevices=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectHome=yes",
        "--property=ProtectSystem=strict",
        f"--property=ReadWritePaths={staging_dir}",
        "tar",
        "--zstd",
        "--extract",
        "--file=-",
        "--directory",
        staging_dir,
        "--no-same-owner",
        "--no-same-permissions",
    ]


def build_mysqldump_argv(db_name: str) -> list[str]:
    return [
        "mysqldump",
        "--single-transaction",
        "--quick",
        "--routines",
        mariadb.validate_identifier(db_name),
    ]


def build_mysql_restore_argv(db_name: str, *, defaults_file: str) -> list[str]:
    absolute = Path(defaults_file).is_absolute() or PurePosixPath(defaults_file).is_absolute()
    if not absolute or "\x00" in defaults_file:
        raise BackupError("Invalid mysql client configuration path")
    return [
        "mysql",
        f"--defaults-extra-file={defaults_file}",
        "--database",
        mariadb.validate_identifier(db_name),
    ]


# --- manifest ----------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(
    directory: Path,
    *,
    backup_id: str,
    domain: str,
    php_version: str,
    wordpress: bool,
    databases: list[str],
) -> dict[str, Any]:
    files = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and p.name != MANIFEST):
        rel = path.relative_to(directory).as_posix()
        files.append({"path": rel, "sha256": sha256_file(path), "size": path.stat().st_size})
    manifest = {
        "version": MANIFEST_VERSION,
        "backup_id": backup_id,
        "domain": domain,
        "created_at": utcnow().isoformat() + "Z",
        "php_version": php_version,
        "wordpress": wordpress,
        "databases": databases,
        "files": files,
        "panel_version": __version__,
        "s3": False,
    }
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def read_manifest(directory: Path) -> dict[str, Any] | None:
    """The manifest for a backup directory, or None if absent/corrupt."""
    try:
        manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or "backup_id" not in manifest:
        return None
    return manifest


def verify_checksums(directory: Path, manifest: dict[str, Any]) -> None:
    """Refuse to restore from a corrupted or tampered backup."""
    for entry in manifest.get("files", []):
        path = directory / entry["path"]
        if not path.is_file():
            raise BackupError(f"Backup is incomplete: missing {entry['path']}")
        if sha256_file(path) != entry["sha256"]:
            raise BackupError(f"Checksum mismatch in {entry['path']} — refusing to restore")


# --- listing and retention -----------------------------------------------------------


@dataclass(frozen=True)
class BackupInfo:
    domain: str
    backup_id: str
    created_at: str
    size_bytes: int
    databases: tuple[str, ...]
    wordpress: bool
    php_version: str
    s3: bool


def _info_from_manifest(manifest: dict[str, Any]) -> BackupInfo:
    return BackupInfo(
        domain=manifest["domain"],
        backup_id=manifest["backup_id"],
        created_at=manifest.get("created_at", ""),
        size_bytes=sum(f.get("size", 0) for f in manifest.get("files", [])),
        databases=tuple(manifest.get("databases", [])),
        wordpress=bool(manifest.get("wordpress", False)),
        php_version=manifest.get("php_version", ""),
        s3=bool(manifest.get("s3", False)),
    )


def list_backups(root: str, domain: str | None = None) -> list[BackupInfo]:
    """Newest-first. Directories without a valid manifest are skipped."""
    base = Path(root)
    if not base.is_dir():
        return []
    domains = [base / domain] if domain else sorted(p for p in base.iterdir() if p.is_dir())
    out: list[BackupInfo] = []
    for domain_dir in domains:
        if not domain_dir.is_dir():
            continue
        for candidate in domain_dir.iterdir():
            if not candidate.is_dir() or not BACKUP_ID_RE.fullmatch(candidate.name):
                continue
            manifest = read_manifest(candidate)
            if manifest is not None:
                out.append(_info_from_manifest(manifest))
    out.sort(key=lambda b: b.backup_id, reverse=True)
    return out


def prune(root: str, domain: str, keep: int) -> list[str]:
    """Delete the oldest local backups beyond `keep`; clean stale staging dirs."""
    domain_dir = Path(root) / domain
    if not domain_dir.is_dir():
        return []
    removed: list[str] = []
    backups = sorted(
        (p for p in domain_dir.iterdir() if p.is_dir() and BACKUP_ID_RE.fullmatch(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    for stale in backups[max(keep, 1) :]:
        shutil.rmtree(stale, ignore_errors=True)
        removed.append(stale.name)
    cutoff = (utcnow() - timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ")
    for staging in domain_dir.glob(".staging-*"):
        if staging.name.removeprefix(".staging-") < cutoff:
            shutil.rmtree(staging, ignore_errors=True)
    if removed:
        log.info("backups_pruned", domain=domain, removed=removed)
    return removed


# --- backup steps (used by the operation pipeline) -----------------------------------


async def _run_or_raise(argv: list[str], what: str, **kwargs: Any) -> None:
    result = await runner.run(argv, **kwargs)
    if not result.ok:
        raise BackupError(f"{what} failed: {result.stderr.strip()[:300] or 'unknown error'}")


async def dump_databases(db_names: list[str], dest: Path) -> None:
    (dest / DB_DIR).mkdir(parents=True, exist_ok=True)
    for name in db_names:
        out = dest / DB_DIR / f"{mariadb.validate_identifier(name)}.sql"
        await _run_or_raise(
            build_mysqldump_argv(name),
            f"mysqldump {name}",
            stdout_path=str(out),
            timeout=1800,
        )


async def archive_files(site_dir: str, dest: Path) -> None:
    await _run_or_raise(
        build_tar_create_argv(dest / FILES_ARCHIVE, site_dir), "archiving files", timeout=3600
    )


def _validate_extracted_tree(root: Path) -> None:
    for current, dir_names, file_names in os.walk(root, followlinks=False):
        for name in [*dir_names, *file_names]:
            path = Path(current) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise BackupError(f"Backup contains an unsupported file type: {path.name}")


async def restore_files(
    directory: Path,
    doc_root: str,
    *,
    sites_root: str,
    staging_root: str,
) -> None:
    archive = directory / FILES_ARCHIVE
    if not archive.is_file():
        raise BackupError("Backup contains no file archive")
    root = Path(staging_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o711)
    if root.is_symlink() or not root.is_dir():
        raise BackupError("Restore staging root must be a real directory")
    root.chmod(0o711)
    staging = Path(tempfile.mkdtemp(prefix="restore-", dir=root))
    try:
        chown = await runner.run(["chown", "hosty-restore:hosty-restore", str(staging)])
        if not chown.ok:
            raise BackupError("Could not prepare the isolated restore directory")
        result = await runner.run(
            build_tar_extract_argv(str(staging)),
            timeout=3600,
            stdin_path=str(archive),
        )
        if not result.ok:
            raise BackupError(f"extracting files failed: {result.stderr.strip()}")
        _validate_extracted_tree(staging)
        source = staging / "public_html"
        if not source.is_dir() or source.is_symlink():
            raise BackupError("Backup contains no valid public_html directory")
        await fs.mirror_import_tree(
            str(source),
            doc_root,
            import_root=str(root),
            sites_root=sites_root,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def restore_databases(directory: Path, db_names: list[str]) -> None:
    for name in db_names:
        dump = directory / DB_DIR / f"{mariadb.validate_identifier(name)}.sql"
        if not dump.is_file():
            raise BackupError(f"Backup contains no dump for database {name}")
        async with mariadb.restricted_client_config(name) as defaults_file:
            await _run_or_raise(
                build_mysql_restore_argv(name, defaults_file=defaults_file),
                f"restoring database {name}",
                stdin_path=str(dump),
                timeout=1800,
            )


# --- S3 mirror -----------------------------------------------------------------------


class S3Like(Protocol):
    async def upload_file(self, local: Path, key: str) -> None: ...
    async def download_file(self, key: str, local: Path) -> None: ...
    async def list_keys(self, prefix: str) -> list[str]: ...


def s3_enabled(settings: Settings) -> bool:
    return bool(
        settings.s3_endpoint
        and settings.s3_bucket
        and settings.s3_access_key
        and settings.s3_secret_key
    )


class S3Client:
    """Minimal boto3 wrapper; sync calls run in a worker thread."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "",
    ) -> None:
        import boto3  # local import: only needed when S3 is configured

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region or None,
        )

    async def upload_file(self, local: Path, key: str) -> None:
        def _upload() -> None:
            self._client.upload_file(str(local), self._bucket, key)  # multipart + retries
            head = self._client.head_object(Bucket=self._bucket, Key=key)
            if int(head["ContentLength"]) != local.stat().st_size:
                raise BackupError(f"S3 size mismatch after uploading {key}")

        try:
            await asyncio.to_thread(_upload)
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError(f"S3 upload of {key} failed: {exc}") from exc

    async def download_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._client.download_file, self._bucket, key, str(local))
        except Exception as exc:
            raise BackupError(f"S3 download of {key} failed: {exc}") from exc

    async def list_keys(self, prefix: str) -> list[str]:
        def _list() -> list[str]:
            keys: list[str] = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys.extend(item["Key"] for item in page.get("Contents", []))
            return keys

        try:
            return await asyncio.to_thread(_list)
        except Exception as exc:
            raise BackupError(f"S3 listing failed: {exc}") from exc


def s3_key(prefix: str, domain: str, backup_id: str, rel_path: str) -> str:
    return f"{prefix.strip('/')}/{domain}/{backup_id}/{rel_path}"


async def upload_backup(
    s3: S3Like, prefix: str, directory: Path, domain: str, backup_id: str
) -> None:
    """Mirror every backup file to S3, then flag the local manifest."""
    manifest = read_manifest(directory)
    if manifest is None:
        raise BackupError("Cannot upload: manifest missing")
    for entry in manifest["files"]:
        await s3.upload_file(
            directory / entry["path"], s3_key(prefix, domain, backup_id, entry["path"])
        )
    manifest["s3"] = True
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    await s3.upload_file(directory / MANIFEST, s3_key(prefix, domain, backup_id, MANIFEST))


async def download_backup(s3: S3Like, prefix: str, root: str, domain: str, backup_id: str) -> Path:
    """Fetch a backup from S3 into the local store (for restore)."""
    target = backup_dir(root, domain, backup_id)
    key_prefix = s3_key(prefix, domain, backup_id, "")
    keys = await s3.list_keys(key_prefix)
    if not keys:
        raise NotFoundError(f"Backup {backup_id} not found in S3")
    target_root = target.resolve()
    for key in keys:
        if not key.startswith(key_prefix):
            raise BackupError("S3 returned an object outside the requested backup prefix")
        relative = PurePosixPath(key[len(key_prefix) :])
        unsafe_parts = any(part in {"", ".", ".."} for part in relative.parts)
        if relative.is_absolute() or not relative.parts or unsafe_parts:
            raise BackupError(f"Unsafe S3 backup object key: {key!r}")
        local = (target / Path(*relative.parts)).resolve()
        try:
            local.relative_to(target_root)
        except ValueError as exc:
            raise BackupError(f"S3 backup object escaped the local target: {key!r}") from exc
        local.parent.mkdir(parents=True, exist_ok=True)
        await s3.download_file(key, local)
    return target


# --- scheduling ----------------------------------------------------------------------


def last_scheduled_time(now: datetime, *, frequency: str, hour: int) -> datetime:
    """The most recent scheduled run time <= now. Weekly anchors to Monday."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if frequency == "weekly":
        candidate -= timedelta(days=candidate.weekday())  # back to Monday
        if candidate > now:
            candidate -= timedelta(days=7)
    elif candidate > now:
        candidate -= timedelta(days=1)
    return candidate


def is_due(now: datetime, *, frequency: str, hour: int, last_run: datetime | None) -> bool:
    if frequency not in FREQUENCIES:
        return False
    return last_run is None or last_run < last_scheduled_time(now, frequency=frequency, hour=hour)
