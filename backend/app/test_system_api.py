from __future__ import annotations

from app.system import systemd


def _fake_status(active: str = "active"):
    async def fake(unit: str) -> systemd.ServiceStatus:
        return systemd.ServiceStatus(unit, True, active, "running", "enabled")

    return fake


async def test_services_require_auth(client):
    assert (await client.get("/api/system/services")).status_code == 401
    assert (await client.get("/api/system/services/caddy")).status_code == 401
    assert (await client.post("/api/system/services/caddy/actions/restart")).status_code == 401


async def test_list_services(admin_client, settings, monkeypatch):
    monkeypatch.setattr(systemd, "status", _fake_status())
    resp = await admin_client.get("/api/system/services")
    assert resp.status_code == 200
    units = [s["unit"] for s in resp.json()]
    assert units == settings.managed_units
    assert all(s["active_state"] == "active" for s in resp.json())


async def test_single_service_status(admin_client, monkeypatch):
    monkeypatch.setattr(systemd, "status", _fake_status("inactive"))
    resp = await admin_client.get("/api/system/services/caddy")
    assert resp.status_code == 200
    assert resp.json() == {
        "unit": "caddy",
        "available": True,
        "active_state": "inactive",
        "sub_state": "running",
        "enabled": "enabled",
    }


async def test_unmanaged_unit_is_404(admin_client):
    resp = await admin_client.get("/api/system/services/sshd")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_unknown_action_is_404(admin_client):
    resp = await admin_client.post("/api/system/services/caddy/actions/explode")
    assert resp.status_code == 404


async def test_service_action_calls_systemd(admin_client, monkeypatch):
    called: list[tuple[str, str]] = []

    async def fake_control(action: str, unit: str) -> systemd.ServiceStatus:
        called.append((action, unit))
        return systemd.ServiceStatus(unit, True, "active", "running", "enabled")

    monkeypatch.setattr(systemd, "control", fake_control)
    resp = await admin_client.post("/api/system/services/caddy/actions/restart")
    assert resp.status_code == 200
    assert called == [("restart", "caddy")]


async def test_systemd_failure_maps_to_502(admin_client, monkeypatch):
    async def fake_control(action: str, unit: str) -> systemd.ServiceStatus:
        raise systemd.SystemdError("systemctl restart caddy failed: boom")

    monkeypatch.setattr(systemd, "control", fake_control)
    resp = await admin_client.post("/api/system/services/caddy/actions/restart")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "systemd_error"
