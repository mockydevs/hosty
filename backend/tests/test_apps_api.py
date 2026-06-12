"""Apps API + provisioning pipeline (docker/fs/caddy fully faked).

Background tasks run before the ASGI transport returns, so operations are
already finished when we poll them — same trick as test_sites_api.py."""

from __future__ import annotations

import pytest

from app.services.caddy import CaddyClient
from app.system import docker, fs
from tests.conftest import setup_and_login  # noqa: F401  (fixture file)

DIGEST = "ghcr.io/org/web@sha256:" + "b" * 64


class FakeDocker:
    """Records every engine mutation; can be told to fail a specific step."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_on: str | None = None
        self.containers: set[str] = set()
        self.networks: set[str] = set()
        self.files: dict[str, str] = {}
        self.caddy_configs: list[dict] = []

    def _maybe_fail(self, step: str) -> None:
        if self.fail_on == step:
            raise docker.DockerError(f"injected failure in {step}")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        async def pull_image(image: str) -> None:
            fake._maybe_fail("image")
            fake.calls.append(("pull", image))

        async def resolve_digest(image: str) -> str | None:
            return DIGEST

        async def run_container(spec: docker.ContainerSpec) -> None:
            fake._maybe_fail("container")
            fake.calls.append(("run", spec))
            fake.containers.add(spec.name)

        async def remove_container(name: str) -> None:
            fake.calls.append(("rm", name))
            fake.containers.discard(name)

        async def start_container(name: str) -> None:
            fake.calls.append(("start", name))

        async def stop_container(name: str) -> None:
            fake.calls.append(("stop", name))

        async def restart_container(name: str) -> None:
            fake.calls.append(("restart", name))

        async def container_logs(name: str, *, tail: int = 200) -> str:
            return f"logs of {name} tail={tail}"

        async def create_network(name: str) -> None:
            fake._maybe_fail("network")
            fake.calls.append(("network_create", name))
            fake.networks.add(name)

        async def remove_network(name: str) -> None:
            fake.calls.append(("network_rm", name))
            fake.networks.discard(name)

        async def create_dir(path: str, *, root: str) -> None:
            fake._maybe_fail("app_dir")
            fake.calls.append(("create_dir", path))

        def write_file(path: str, content: str, *, root: str) -> None:
            fake.files[path] = content

        async def remove_tree(path: str, *, root: str) -> None:
            fake.calls.append(("remove_tree", path))

        async def caddy_apply(self_client, config: dict) -> None:
            fake._maybe_fail("caddy")
            fake.calls.append(("caddy_apply", len(fake.caddy_configs)))
            fake.caddy_configs.append(config)

        monkeypatch.setattr(docker, "pull_image", pull_image)
        monkeypatch.setattr(docker, "resolve_digest", resolve_digest)
        monkeypatch.setattr(docker, "run_container", run_container)
        monkeypatch.setattr(docker, "remove_container", remove_container)
        monkeypatch.setattr(docker, "start_container", start_container)
        monkeypatch.setattr(docker, "stop_container", stop_container)
        monkeypatch.setattr(docker, "restart_container", restart_container)
        monkeypatch.setattr(docker, "container_logs", container_logs)
        monkeypatch.setattr(docker, "create_network", create_network)
        monkeypatch.setattr(docker, "remove_network", remove_network)
        monkeypatch.setattr(fs, "create_dir", create_dir)
        monkeypatch.setattr(fs, "write_file", write_file)
        monkeypatch.setattr(fs, "remove_tree", remove_tree)
        monkeypatch.setattr(CaddyClient, "apply", caddy_apply)


@pytest.fixture
def fake_docker(monkeypatch) -> FakeDocker:
    fake = FakeDocker()
    fake.install(monkeypatch)
    return fake


CREATE_BODY = {
    "name": "web",
    "domain": "app.example.com",
    "image": "ghcr.io/org/web:v1",
    "internal_port": 3000,
    "env": {"NODE_ENV": "production", "SECRET": "s3cret"},
    "volumes": [{"name": "data", "mount_path": "/data"}],
    "memory_mb": 512,
}


async def create_app_ok(client, fake_docker) -> tuple[dict, dict]:
    resp = await client.post("/api/apps", json=CREATE_BODY)
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    op = await client.get(f"/api/operations/{payload['operation_id']}")
    assert op.status_code == 200
    return payload, op.json()


async def test_create_app_happy_path(admin_client, fake_docker):
    payload, op = await create_app_ok(admin_client, fake_docker)
    assert op["status"] == "succeeded", op
    assert all(s["status"] == "done" for s in op["steps"])

    app = (await admin_client.get(f"/api/apps/{payload['app']['id']}")).json()
    assert app["status"] == "running"
    assert app["image_digest"] == DIGEST
    assert 20100 <= app["host_port"] <= 29999

    container_id = f"hosty-app-{app['id']}"
    assert container_id in fake_docker.containers
    assert f"hosty-net-{app['id']}" in fake_docker.networks
    # The container runs the resolved digest, not the floating tag.
    run_spec = next(c[1] for c in fake_docker.calls if c[0] == "run")
    assert run_spec.image == DIGEST
    assert run_spec.host_port == app["host_port"]
    # Env reached the env file, not argv.
    env_files = [c for c in fake_docker.files if c.endswith("/env")]
    assert env_files and "NODE_ENV=production" in fake_docker.files[env_files[0]]
    # The published Caddy config routes the domain to the loopback port.
    assert any(
        "app.example.com" in str(cfg) and f"127.0.0.1:{app['host_port']}" in str(cfg)
        for cfg in fake_docker.caddy_configs
    )


async def test_create_app_failure_rolls_back(admin_client, fake_docker):
    fake_docker.fail_on = "container"
    resp = await admin_client.post("/api/apps", json=CREATE_BODY)
    assert resp.status_code == 202
    payload = resp.json()
    op = (await admin_client.get(f"/api/operations/{payload['operation_id']}")).json()
    assert op["status"] == "failed"

    app = (await admin_client.get(f"/api/apps/{payload['app']['id']}")).json()
    assert app["status"] == "error"
    assert "container" in app["error_message"]
    # Compensation: network and app dir are gone, no container left behind.
    assert not fake_docker.networks
    assert not fake_docker.containers
    assert any(c[0] == "remove_tree" for c in fake_docker.calls)


async def test_delete_app(admin_client, fake_docker):
    payload, _ = await create_app_ok(admin_client, fake_docker)
    app_id = payload["app"]["id"]

    resp = await admin_client.request("DELETE", f"/api/apps/{app_id}", json={"confirm_name": "WEB"})
    assert resp.status_code == 202, resp.text
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded", op
    assert not fake_docker.containers
    assert not fake_docker.networks
    assert (await admin_client.get(f"/api/apps/{app_id}")).status_code == 404


async def test_delete_requires_matching_confirmation(admin_client, fake_docker):
    payload, _ = await create_app_ok(admin_client, fake_docker)
    resp = await admin_client.request(
        "DELETE", f"/api/apps/{payload['app']['id']}", json={"confirm_name": "nope"}
    )
    assert resp.status_code == 409


async def test_domain_collision_with_site_is_refused(admin_client, fake_docker, monkeypatch):
    # Make site creation cheap: fake system already covers caddy; reuse it via
    # an app on the same domain after a site exists.
    from tests.test_sites_api import FakeSystem

    FakeSystem().install(monkeypatch)
    resp = await admin_client.post("/api/sites", json={"domain": "app.example.com"})
    assert resp.status_code == 202
    resp = await admin_client.post("/api/apps", json=CREATE_BODY)
    assert resp.status_code == 409
    assert "site" in resp.json()["error"]["message"].lower()


async def test_duplicate_app_name_is_refused(admin_client, fake_docker):
    await create_app_ok(admin_client, fake_docker)
    body = dict(CREATE_BODY, domain="other.example.com")
    resp = await admin_client.post("/api/apps", json=body)
    assert resp.status_code == 409


async def test_invalid_image_is_rejected(admin_client, fake_docker):
    body = dict(CREATE_BODY, image="--privileged")
    resp = await admin_client.post("/api/apps", json=body)
    assert resp.status_code == 422


async def test_lifecycle_and_logs(admin_client, fake_docker):
    payload, _ = await create_app_ok(admin_client, fake_docker)
    app_id = payload["app"]["id"]

    resp = await admin_client.post(f"/api/apps/{app_id}/stop")
    assert resp.status_code == 200 and resp.json()["status"] == "stopped"
    resp = await admin_client.post(f"/api/apps/{app_id}/start")
    assert resp.status_code == 200 and resp.json()["status"] == "running"
    resp = await admin_client.post(f"/api/apps/{app_id}/restart")
    assert resp.status_code == 200 and resp.json()["status"] == "running"

    resp = await admin_client.get(f"/api/apps/{app_id}/logs?tail=50")
    assert resp.status_code == 200
    assert f"hosty-app-{app_id}" in resp.json()["logs"]


async def test_quota_enforced_for_clients(admin_client, fake_docker):
    # Create a client with max_apps=1.
    resp = await admin_client.post("/api/users", json={"username": "tenant", "role": "client"})
    assert resp.status_code == 201, resp.text
    temp_password = resp.json()["temp_password"]
    user_id = resp.json()["user"]["id"]
    resp = await admin_client.patch(f"/api/users/{user_id}", json={"max_apps": 1})
    assert resp.status_code == 200, resp.text

    # Log in as the client (forced password change first).
    login = await admin_client.post(
        "/api/auth/login", json={"username": "tenant", "password": temp_password}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    resp = await admin_client.post(
        "/api/auth/change-password",
        json={"current_password": temp_password, "new_password": "a-long-password-123"},
        headers=headers,
    )
    assert resp.status_code == 204, resp.text
    login = await admin_client.post(
        "/api/auth/login", json={"username": "tenant", "password": "a-long-password-123"}
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await admin_client.post("/api/apps", json=CREATE_BODY, headers=headers)
    assert resp.status_code == 202, resp.text
    body = dict(CREATE_BODY, name="web2", domain="two.example.com")
    resp = await admin_client.post("/api/apps", json=body, headers=headers)
    assert resp.status_code == 409
    assert "quota" in resp.json()["error"]["message"].lower()


async def test_clients_cannot_see_others_apps(admin_client, fake_docker):
    payload, _ = await create_app_ok(admin_client, fake_docker)
    app_id = payload["app"]["id"]

    resp = await admin_client.post("/api/users", json={"username": "peeker", "role": "client"})
    temp_password = resp.json()["temp_password"]
    login = await admin_client.post(
        "/api/auth/login", json={"username": "peeker", "password": temp_password}
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    resp = await admin_client.post(
        "/api/auth/change-password",
        json={"current_password": temp_password, "new_password": "a-long-password-123"},
        headers=headers,
    )
    login = await admin_client.post(
        "/api/auth/login", json={"username": "peeker", "password": "a-long-password-123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (await admin_client.get("/api/apps", headers=headers)).json() == []
    # 404, not 403 — existence must not leak.
    assert (await admin_client.get(f"/api/apps/{app_id}", headers=headers)).status_code == 404
