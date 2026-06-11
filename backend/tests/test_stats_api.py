from __future__ import annotations


async def test_stats_require_auth(client):
    assert (await client.get("/api/system/stats")).status_code == 401


async def test_stats_shape(admin_client):
    resp = await admin_client.get("/api/system/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "cpu_percent",
        "load_avg",
        "memory_total",
        "memory_used",
        "memory_percent",
        "disk_total",
        "disk_used",
        "disk_percent",
        "uptime_seconds",
    }
    assert len(body["load_avg"]) == 3
    assert 0 <= body["memory_percent"] <= 100
    assert 0 <= body["disk_percent"] <= 100
    assert body["memory_total"] > 0
    assert body["disk_total"] > 0
    assert body["uptime_seconds"] >= 0
