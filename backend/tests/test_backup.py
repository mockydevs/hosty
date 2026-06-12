from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.core.errors import NotFoundError
from app.services import backup
from app.system import runner

# --- ids ---------------------------------------------------------------------------


def test_backup_id_roundtrip():
    bid = backup.new_backup_id(datetime(2026, 6, 11, 4, 30, 9))
    assert bid == "20260611T043009Z"
    assert backup.validate_backup_id(bid) == bid


@pytest.mark.parametrize("bad", ["", "..", "x/y", "20260611", "20260611T043009Z/..", "latest"])
def test_backup_id_rejects_garbage(bad):
    with pytest.raises(NotFoundError):
        backup.validate_backup_id(bad)


# --- argv builders -------------------------------------------------------------------


def test_tar_argv_exact(tmp_path):
    archive = tmp_path / "files.tar.zst"
    assert backup.build_tar_create_argv(archive, "/var/www/example.com") == [
        "tar",
        "--zstd",
        "-cf",
        str(archive),
        "-C",
        "/var/www/example.com",
        ".",
    ]
    assert backup.build_tar_extract_argv("/var/lib/hosty/restore-staging/job") == [
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
        "--property=ReadWritePaths=/var/lib/hosty/restore-staging/job",
        "tar",
        "--zstd",
        "--extract",
        "--file=-",
        "--directory",
        "/var/lib/hosty/restore-staging/job",
        "--no-same-owner",
        "--no-same-permissions",
    ]


def test_mysql_argv_exact():
    assert backup.build_mysqldump_argv("shop_db") == [
        "mysqldump",
        "--single-transaction",
        "--quick",
        "--routines",
        "shop_db",
    ]
    assert backup.build_mysql_restore_argv("shop_db", defaults_file="/tmp/mysql.cnf") == [
        "mysql",
        "--defaults-extra-file=/tmp/mysql.cnf",
        "--database",
        "shop_db",
    ]


def test_mysql_argv_rejects_bad_identifier():
    with pytest.raises(Exception):  # noqa: B017 - mariadb's validation error type
        backup.build_mysqldump_argv("shop;drop")


# --- manifest ------------------------------------------------------------------------


def _make_backup_dir(directory: Path, *, databases: list[str] | None = None) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / backup.FILES_ARCHIVE).write_bytes(b"FAKE-ZSTD-ARCHIVE" * 64)
    (directory / backup.DB_DIR).mkdir(exist_ok=True)
    for name in databases or []:
        (directory / backup.DB_DIR / f"{name}.sql").write_text(f"-- dump {name}\n")
    return backup.write_manifest(
        directory,
        backup_id=directory.name,
        domain=directory.parent.name,
        php_version="8.3",
        wordpress=False,
        databases=databases or [],
    )


def test_manifest_roundtrip_and_checksums(tmp_path):
    directory = tmp_path / "example.com" / "20260611T030000Z"
    manifest = _make_backup_dir(directory, databases=["shop_db"])
    assert {f["path"] for f in manifest["files"]} == {"files.tar.zst", "db/shop_db.sql"}
    assert backup.read_manifest(directory) == manifest
    backup.verify_checksums(directory, manifest)  # must not raise

    # corrupt the archive -> verification refuses
    (directory / backup.FILES_ARCHIVE).write_bytes(b"TAMPERED")
    with pytest.raises(backup.BackupError, match="Checksum mismatch"):
        backup.verify_checksums(directory, manifest)


def test_read_manifest_handles_missing_and_corrupt(tmp_path):
    assert backup.read_manifest(tmp_path) is None
    (tmp_path / backup.MANIFEST).write_text("{not json")
    assert backup.read_manifest(tmp_path) is None


# --- listing & retention --------------------------------------------------------------


def test_list_backups_sorts_and_skips_incomplete(tmp_path):
    _make_backup_dir(tmp_path / "a.com" / "20260610T030000Z")
    _make_backup_dir(tmp_path / "a.com" / "20260611T030000Z")
    (tmp_path / "a.com" / ".staging-20260612T030000Z").mkdir(parents=True)  # ignored
    (tmp_path / "a.com" / "20260609T030000Z").mkdir()  # no manifest -> ignored
    _make_backup_dir(tmp_path / "b.com" / "20260608T030000Z")

    all_backups = backup.list_backups(str(tmp_path))
    assert [(b.domain, b.backup_id) for b in all_backups] == [
        ("a.com", "20260611T030000Z"),
        ("a.com", "20260610T030000Z"),
        ("b.com", "20260608T030000Z"),
    ]
    assert [b.backup_id for b in backup.list_backups(str(tmp_path), "b.com")] == [
        "20260608T030000Z"
    ]
    assert backup.list_backups(str(tmp_path / "missing")) == []


def test_prune_keeps_newest(tmp_path):
    for day in (9, 10, 11):
        _make_backup_dir(tmp_path / "a.com" / f"202606{day:02d}T030000Z")
    removed = backup.prune(str(tmp_path), "a.com", keep=2)
    assert removed == ["20260609T030000Z"]
    assert [b.backup_id for b in backup.list_backups(str(tmp_path), "a.com")] == [
        "20260611T030000Z",
        "20260610T030000Z",
    ]


# --- scheduling ----------------------------------------------------------------------

NOW = datetime(2026, 6, 11, 4, 30)  # a Thursday


@pytest.mark.parametrize(
    "frequency,hour,last_run,expected",
    [
        ("daily", 3, None, True),  # never ran
        ("daily", 3, datetime(2026, 6, 11, 3, 5), False),  # ran after today's slot
        ("daily", 3, datetime(2026, 6, 10, 3, 5), True),  # ran yesterday
        ("daily", 5, datetime(2026, 6, 10, 5, 30), False),  # today's slot not reached yet
        ("daily", 5, datetime(2026, 6, 9, 5, 30), True),  # missed yesterday's slot
        ("weekly", 3, datetime(2026, 6, 7, 12, 0), True),  # before Monday's slot
        ("weekly", 3, datetime(2026, 6, 8, 4, 0), False),  # ran after Monday's slot
        ("hourly", 3, None, False),  # unknown frequency -> never due
    ],
)
def test_is_due(frequency, hour, last_run, expected):
    assert backup.is_due(NOW, frequency=frequency, hour=hour, last_run=last_run) is expected


def test_last_scheduled_time_weekly_anchors_to_monday():
    t = backup.last_scheduled_time(NOW, frequency="weekly", hour=3)
    assert t == datetime(2026, 6, 8, 3, 0)  # Monday this week
    early_monday = datetime(2026, 6, 8, 1, 0)
    t = backup.last_scheduled_time(early_monday, frequency="weekly", hour=3)
    assert t == datetime(2026, 6, 1, 3, 0)  # slot not reached -> previous Monday


# --- dump/archive/restore via the (faked) runner --------------------------------------


def make_fake_runner(calls: list[list[str]]):
    async def fake_run(
        argv, *, timeout=30.0, cwd=None, env=None, stdout_path=None, stdin_path=None
    ):
        calls.append(list(argv))
        if argv[0] == "mysqldump" and stdout_path:
            Path(stdout_path).write_text(f"-- dump of {argv[-1]}\n")
        if argv[0] == "tar" and "-cf" in argv:
            Path(argv[argv.index("-cf") + 1]).write_bytes(b"FAKE-TAR-CONTENT" * 16)
        if argv[0] == "systemd-run":
            staging_arg = next(arg for arg in argv if arg.startswith("--property=ReadWritePaths="))
            staging = Path(staging_arg.split("=", 2)[2])
            (staging / "public_html").mkdir()
            (staging / "public_html" / "index.php").write_text("restored")
        return runner.CommandResult(tuple(argv), 0, "", "", 1.0)

    return fake_run


async def test_dump_and_archive_create_files(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("app.system.runner.run", make_fake_runner(calls))
    await backup.dump_databases(["shop_db"], tmp_path)
    await backup.archive_files("/var/www/a.com", tmp_path)
    assert (tmp_path / "db" / "shop_db.sql").read_text().startswith("-- dump")
    assert (tmp_path / backup.FILES_ARCHIVE).exists()
    assert calls[0][0] == "mysqldump"
    assert calls[1][:3] == ["tar", "--zstd", "-cf"]


async def test_restore_runs_extract_and_mysql(tmp_path, monkeypatch):
    directory = tmp_path / "a.com" / "20260611T030000Z"
    _make_backup_dir(directory, databases=["shop_db"])
    calls: list[list[str]] = []
    monkeypatch.setattr("app.system.runner.run", make_fake_runner(calls))
    await backup.restore_files(
        directory,
        "/var/www/a.com/public_html",
        sites_root="/var/www",
        staging_root=str(tmp_path / "restore-staging"),
    )
    await backup.restore_databases(directory, ["shop_db"])
    assert calls[0][0] == "chown"
    assert calls[1][0] == "systemd-run"
    assert calls[2][0] == "rsync"
    assert calls[3][0] == "mariadb"  # create restricted restore user
    assert calls[4][0] == "mysql" and calls[4][-1] == "shop_db"
    assert calls[5][0] == "mariadb"  # drop restricted restore user


async def test_restore_missing_dump_fails(tmp_path):
    directory = tmp_path / "a.com" / "20260611T030000Z"
    _make_backup_dir(directory, databases=[])
    with pytest.raises(backup.BackupError, match="no dump"):
        await backup.restore_databases(directory, ["ghost_db"])


# --- S3 mirror -----------------------------------------------------------------------


class FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, bytes] = objects or {}

    async def upload_file(self, local: Path, key: str) -> None:
        self.objects[key] = local.read_bytes()

    async def download_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.objects[key])

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


def _s3_settings(settings, tmp_path):
    return settings.model_copy(
        update={
            "backups_root": str(tmp_path),
            "s3_endpoint": "http://127.0.0.1:9000",
            "s3_bucket": "backups",
            "s3_access_key": "k",
            "s3_secret_key": "s",
        }
    )


async def test_upload_backup_mirrors_and_flags(tmp_path, settings):
    directory = tmp_path / "a.com" / "20260611T030000Z"
    _make_backup_dir(directory, databases=["shop_db"])
    fake = FakeS3()
    await backup.upload_backup(fake, "hosty", directory, "a.com", "20260611T030000Z")
    assert set(fake.objects) == {
        "hosty/a.com/20260611T030000Z/files.tar.zst",
        "hosty/a.com/20260611T030000Z/db/shop_db.sql",
        "hosty/a.com/20260611T030000Z/manifest.json",
    }
    manifest = backup.read_manifest(directory)
    assert manifest is not None and manifest["s3"] is True


async def test_download_backup_restores_local_copy(tmp_path, settings):
    source = tmp_path / "a.com" / "20260611T030000Z"
    _make_backup_dir(source, databases=["shop_db"])
    fake = FakeS3()
    await backup.upload_backup(fake, "hosty", source, "a.com", "20260611T030000Z")

    other_root = tmp_path / "elsewhere"
    target = await backup.download_backup(
        fake, "hosty", str(other_root), "a.com", "20260611T030000Z"
    )
    manifest = backup.read_manifest(target)
    assert manifest is not None
    backup.verify_checksums(target, manifest)

    with pytest.raises(NotFoundError):
        await backup.download_backup(fake, "hosty", str(other_root), "a.com", "20260101T000000Z")


async def test_download_backup_rejects_traversal_keys(tmp_path):
    prefix = "hosty/a.com/20260611T030000Z/"
    fake = FakeS3({prefix + "../../escape": b"owned"})
    with pytest.raises(backup.BackupError, match="Unsafe S3"):
        await backup.download_backup(
            fake, "hosty", str(tmp_path / "backups"), "a.com", "20260611T030000Z"
        )
    assert not (tmp_path / "escape").exists()


def test_s3_enabled_requires_all_settings(settings):
    assert backup.s3_enabled(settings) is False
    assert backup.s3_enabled(_s3_settings(settings, Path("/tmp"))) is True


# --- DB secret encryption -------------------------------------------------------------


def test_secret_encryption_roundtrip():
    from app.core import secrets

    token = secrets.encrypt_secret("super-secret-key", "panel-secret")
    assert "super-secret-key" not in token
    assert secrets.decrypt_secret(token, "panel-secret") == "super-secret-key"
    with pytest.raises(secrets.SecretDecryptionError):
        secrets.decrypt_secret(token, "a-different-panel-secret")
