from __future__ import annotations


async def test_health_ok(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"]


async def test_security_headers_present(client):
    resp = await client.get("/api/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Content-Security-Policy"] == "default-src 'self'"
    assert "X-Request-ID" in resp.headers
