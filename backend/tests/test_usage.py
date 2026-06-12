"""Usage metering (Phase 11c/11d): log parsing, du argv, CSV export, API scoping."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.services import usage
from app.services.usage import ClientUsage
from tests.test_multi_tenancy import make_active_client

# --- bandwidth log parsing (pure) ---------------------------------------------------


def _log_line(host: str, size: int, ts: float) -> str:
    return json.dumps({"ts": ts, "size": size, "request": {"host": host}})


JUNE_2026 = datetime(2026, 6, 11, tzinfo=timezone.utc).timestamp()
MAY_2026 = datetime(2026, 5, 8, tzinfo=timezone.utc).timestamp()


def test_parse_bandwidth_log_sums_per_host(tmp_path):
    p = tmp_path / "access.log"
    p.write_text(
        "\n".join(
            [
                _log_line("a.example", 1000, JUNE_2026),
                _log_line("a.example", 500, JUNE_2026 + 60),
                _log_line("b.example", 42, JUNE_2026),
                "not json at all",
                json.dumps({"ts": JUNE_2026}),  # missing request/host
                json.dumps({"ts": "NaNsense", "request": {"host": "a.example"}}),
            ]
        ),
        encoding="utf-8",
    )
    assert usage.parse_bandwidth_log(str(p)) == {"a.example": 1500, "b.example": 42}


def test_parse_bandwidth_log_month_filter(tmp_path):
    p = tmp_path / "access.log"
    p.write_text(
        _log_line("a.example", 100, MAY_2026) + "\n" + _log_line("a.example", 7, JUNE_2026),
        encoding="utf-8",
    )
    assert usage.parse_bandwidth_log(str(p), month="2026-05") == {"a.example": 100}
    assert usage.parse_bandwidth_log(str(p), month="2026-06") == {"a.example": 7}
    assert usage.parse_bandwidth_log(str(p)) == {"a.example": 107}


def test_parse_bandwidth_log_missing_file():
    assert usage.parse_bandwidth_log("/nonexistent/access.log") == {}


# --- du argv (injection surface) ------------------------------------------------------


def test_build_du_argv_validates_path():
    argv = usage.build_du_argv("/srv/sites/blog.example", root="/srv/sites")
    assert argv[:3] == ["du", "-sb", "--"]

    import pytest

    from app.system.fs import InvalidSitePathError

    with pytest.raises(InvalidSitePathError):
        usage.build_du_argv("/etc/passwd", root="/srv/sites")


# --- CSV export -----------------------------------------------------------------------


def test_usage_csv():
    rows = [
        ClientUsage(
            user_id=1,
            username="alice",
            site_count=2,
            disk_bytes=3 * 1024 * 1024,
            db_bytes=1024 * 1024,
            bandwidth_bytes=10 * 1024 * 1024,
            max_disk_mb=100,
        )
    ]
    out = usage.usage_csv(rows, month="2026-06")
    assert out.splitlines() == [
        "username,month,sites,disk_mb,db_mb,bandwidth_mb",
        "alice,2026-06,2,3,1,10",
    ]


# --- API scoping ----------------------------------------------------------------------


async def test_my_usage_empty_client(admin_client, client):
    headers = await make_active_client(admin_client, client, max_sites=3)
    resp = await client.get("/api/usage/me", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sites"] == []
    assert body["disk_bytes"] == 0
    assert body["max_sites"] == 3


async def test_all_usage_is_admin_only(admin_client, client):
    headers = await make_active_client(admin_client, client)
    assert (await client.get("/api/usage", headers=headers)).status_code == 403
    assert (await client.get("/api/usage/export", headers=headers)).status_code == 403
    resp = await admin_client.get("/api/usage")
    assert resp.status_code == 200
    usernames = {r["username"] for r in resp.json()}
    assert "alice" in usernames


async def test_usage_export_csv(admin_client):
    resp = await admin_client.get("/api/usage/export?month=2026-06")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.text.startswith("username,month,sites,disk_mb,db_mb,bandwidth_mb")


async def test_usage_month_validation(admin_client):
    assert (await admin_client.get("/api/usage?month=junk")).status_code == 422
    assert (await admin_client.get("/api/usage/me?month=2026-13")).status_code == 422
