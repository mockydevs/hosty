"""Site import (Phase 11d): archive validation + upload/start route guards."""

from __future__ import annotations

import tarfile
import zipfile

import pytest
import pytest_asyncio

from app.services import site_import
from app.services.site_import import ImportError_
from tests.test_sites_api import FakeSystem

# --- archive member validation (the security-relevant part) ---------------------------


def test_member_validation_rejects_traversal_and_absolute():
    for bad in ("../evil", "a/../../evil", "/etc/passwd", "\\windows\\evil", "a/../b"):
        with pytest.raises(ImportError_):
            site_import._validate_member_name(bad)
    assert site_import._validate_member_name("wp-content/uploads/x.png")


def test_extract_tar_gz(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.php").write_text("<?php echo 'hi';", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "a.txt").write_text("a", encoding="utf-8")
    archive = tmp_path / "site.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src / "index.php", arcname="index.php")
        tar.add(src / "sub" / "a.txt", arcname="sub/a.txt")

    dest = tmp_path / "dest"
    dest.mkdir()
    count = site_import.extract_archive(str(archive), str(dest))
    assert count == 2
    assert (dest / "index.php").read_text(encoding="utf-8") == "<?php echo 'hi';"
    assert (dest / "sub" / "a.txt").exists()


def test_extract_zip(tmp_path):
    archive = tmp_path / "site.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("index.html", "<h1>hi</h1>")
    dest = tmp_path / "dest"
    dest.mkdir()
    assert site_import.extract_archive(str(archive), str(dest)) == 1
    assert (dest / "index.html").exists()


def test_extract_zip_with_traversal_writes_nothing(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ok.txt", "fine")
        zf.writestr("../escape.txt", "evil")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(ImportError_):
        site_import.extract_archive(str(archive), str(dest))
    assert list(dest.iterdir()) == []  # validation happens before extraction


def test_extract_tar_with_traversal_member_rejected(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="../escape.txt")  # tar.add() would sanitize; forge it
        info.size = 1
        import io

        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ImportError_):
        site_import.extract_archive(str(archive), str(tmp_path / "dest"))


def test_extract_tar_rejects_symlinks_before_writing(tmp_path):
    archive = tmp_path / "link.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc"
        tar.addfile(info)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(ImportError_, match="unsupported"):
        site_import.extract_archive(str(archive), str(dest))
    assert list(dest.iterdir()) == []


def test_extract_zip_rejects_symlinks_and_expansion_limits(tmp_path):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        link = zipfile.ZipInfo("escape")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        zf.writestr(link, "/etc")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(ImportError_, match="symbolic link"):
        site_import.extract_archive(str(archive), str(dest))

    large = tmp_path / "large.zip"
    with zipfile.ZipFile(large, "w") as zf:
        zf.writestr("large.bin", b"12345")
    with pytest.raises(ImportError_, match="expansion limit"):
        site_import.extract_archive(str(large), str(dest), max_expanded_bytes=4)


def test_extract_rejects_symlink_destination(tmp_path):
    archive = tmp_path / "site.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("index.html", "ok")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "dest"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(ImportError_, match="symlink"):
        site_import.extract_archive(str(archive), str(link))


def test_unsupported_archive_type(tmp_path):
    p = tmp_path / "site.rar"
    p.write_bytes(b"whatever")
    with pytest.raises(ImportError_):
        site_import.extract_archive(str(p), str(tmp_path))


def test_import_steps_combinations():
    names = [
        s[0] for s in site_import.import_steps(with_files=True, with_sql=True, with_replace=True)
    ]
    assert names == ["files", "database", "search_replace", "finalize"]
    names = [
        s[0] for s in site_import.import_steps(with_files=False, with_sql=True, with_replace=False)
    ]
    assert names == ["database", "finalize"]


# --- API guards -------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


async def _make_site(admin_client) -> int:
    resp = await admin_client.post("/api/sites", json={"domain": "imp.example"})
    assert resp.status_code == 202, resp.text
    return resp.json()["site"]["id"]


async def test_upload_validates_kind_and_filename(admin_client, fake_system, app, tmp_path):
    app.state.settings.uploads_dir = str(tmp_path)
    site_id = await _make_site(admin_client)

    resp = await admin_client.post(
        f"/api/sites/{site_id}/import/upload?kind=files&filename=site.exe", content=b"x"
    )
    assert resp.status_code == 409

    resp = await admin_client.post(
        f"/api/sites/{site_id}/import/upload?kind=nonsense", content=b"x"
    )
    assert resp.status_code == 404

    resp = await admin_client.post(f"/api/sites/{site_id}/import/upload?kind=sql", content=b"")
    assert resp.status_code == 409  # empty body

    resp = await admin_client.post(
        f"/api/sites/{site_id}/import/upload?kind=files&filename=site.tar.gz",
        content=b"not-really-a-tarball",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["size_bytes"] == len(b"not-really-a-tarball")
    assert body["upload_id"].endswith(".files.tar.gz")


async def test_upload_enforces_streaming_size_cap_and_cleans_partial_file(
    admin_client, fake_system, app, tmp_path
):
    app.state.settings.uploads_dir = str(tmp_path)
    app.state.settings.max_import_upload_bytes = 4
    site_id = await _make_site(admin_client)
    response = await admin_client.post(
        f"/api/sites/{site_id}/import/upload?kind=sql", content=b"12345"
    )
    assert response.status_code == 409
    assert not list(tmp_path.rglob("*.sql"))


async def test_start_import_requires_uploads_and_target_db(
    admin_client, fake_system, app, tmp_path
):
    app.state.settings.uploads_dir = str(tmp_path)
    site_id = await _make_site(admin_client)

    # Neither upload id -> 409.
    resp = await admin_client.post(f"/api/sites/{site_id}/import", json={})
    assert resp.status_code == 409

    # Unknown/invalid upload ids -> 404 (no path probing possible).
    resp = await admin_client.post(
        f"/api/sites/{site_id}/import", json={"files_upload_id": "../../etc/passwd"}
    )
    assert resp.status_code == 404
    resp = await admin_client.post(
        f"/api/sites/{site_id}/import", json={"files_upload_id": "0" * 32 + ".files.tar.gz"}
    )
    assert resp.status_code == 404

    # SQL without target_db -> 409.
    up = await admin_client.post(
        f"/api/sites/{site_id}/import/upload?kind=sql", content=b"SELECT 1;"
    )
    resp = await admin_client.post(
        f"/api/sites/{site_id}/import", json={"sql_upload_id": up.json()["upload_id"]}
    )
    assert resp.status_code == 409
