"""M4 gate (v2, ADR-013): the Stacks API against the fully wired
reconciler with a FakeHost at the adapter seams — the real blueprint
renders, real rows persist, the real planner/executor converge, and the
fake Caddy admin captures the published ingress config.

Covers: blueprint catalog, create happy path, undo-free failure →
degraded (and self-heal on the next cycle), quota, 404-scoping, delete,
action dispatch with show-once, logs, owner suspension."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from app.db.models import Stack
from app.domain.specs import ServiceSpec, StackSpec
from app.orchestration.blueprints import base as blueprints_base
from app.orchestration.blueprints.base import ActionResult, Allocation, StackHealth
from app.services import stacks as stacks_service
from app.system import systemd_user as systemd_user_mod
from tests.test_orchestration import FakeHost

CREATE_BODY = {
    "name": "blog",
    "blueprint_id": "raw-image",
    "inputs": {
        "image": "ghcr.io/org/web:v1",
        "internal_port": 3000,
        "domain": "app.example.com",
        "env": {"NODE_ENV": "production", "SECRET": "s3cret"},
        "volumes": {"data": "/data"},
        "memory_mb": 512,
    },
}


class FakeCaddy:
    def __init__(self) -> None:
        self.configs: list[dict] = []

    async def apply(self, config: dict) -> None:
        self.configs.append(config)


@pytest.fixture
def stack_host(app, settings, monkeypatch) -> FakeHost:
    """FakeHost seams + the production reconciler wiring (DB-backed desired
    state, status projection) with observe and Caddy injected."""
    host = FakeHost()
    host.install(monkeypatch)
    host.caddy = FakeCaddy()

    async def observe(db):
        return host.build_observed()

    settings.reconcile_concurrency = 1  # sqlite: one writer
    app.state.reconciler = stacks_service.build_reconciler(
        app.state.sessionmaker, settings, observe=observe, caddy_client=host.caddy
    )
    return host


async def create_stack_ok(client, body: dict | None = None) -> tuple[dict, dict]:
    resp = await client.post("/api/stacks", json=body or CREATE_BODY)
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    op = await client.get(f"/api/operations/{payload['operation_id']}")
    assert op.status_code == 200
    return payload, op.json()


async def make_client_headers(admin_client, username: str, **limits) -> dict:
    """Create a client user, walk the forced password change, return auth
    headers (pattern from test_apps_api)."""
    resp = await admin_client.post("/api/users", json={"username": username, "role": "client"})
    assert resp.status_code == 201, resp.text
    temp_password = resp.json()["temp_password"]
    user_id = resp.json()["user"]["id"]
    if limits:
        resp = await admin_client.patch(f"/api/users/{user_id}", json=limits)
        assert resp.status_code == 200, resp.text
    login = await admin_client.post(
        "/api/auth/login", json={"username": username, "password": temp_password}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await admin_client.post(
        "/api/auth/change-password",
        json={"current_password": temp_password, "new_password": "a-long-password-123"},
        headers=headers,
    )
    assert resp.status_code == 204, resp.text
    login = await admin_client.post(
        "/api/auth/login", json={"username": username, "password": "a-long-password-123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_blueprint_catalog(admin_client, stack_host):
    resp = await admin_client.get("/api/stacks/blueprints")
    assert resp.status_code == 200
    catalog = {bp["id"]: bp for bp in resp.json()}
    assert "raw-image" in catalog
    for blueprint_id in (
        "adminer",
        "grafana",
        "meilisearch",
        "nginx",
        "phpmyadmin",
        "prometheus",
        "rabbitmq",
    ):
        assert blueprint_id in catalog
    schema = catalog["raw-image"]["inputs_schema"]
    assert "image" in schema["properties"]
    assert "internal_port" in schema["properties"]


async def test_blueprint_catalog_keeps_pinned_templates_static(admin_client, stack_host):
    """Pinned templates should not expose a version selector that no longer
    changes the digest-pinned runtime image."""
    resp = await admin_client.get("/api/stacks/blueprints")
    assert resp.status_code == 200
    catalog = {bp["id"]: bp for bp in resp.json()}

    assert "version" not in catalog["postgres"]["inputs_schema"]["properties"]
    assert "version" not in catalog["mongodb"]["inputs_schema"]["properties"]
    # A blueprint without dynamic versions is untouched (no enum on free text).
    assert "enum" not in catalog["raw-image"]["inputs_schema"]["properties"]["image"]


async def test_create_stack_happy_path(admin_client, stack_host):
    payload, op = await create_stack_ok(admin_client)
    assert op["status"] == "succeeded", op
    assert all(s["status"] == "done" for s in op["steps"])

    stack = (await admin_client.get(f"/api/stacks/{payload['stack']['id']}")).json()
    assert stack["status"] == "ready"
    assert stack["observed_generation"] == stack["generation"] == 1
    web = stack["services"][0]
    assert web["name"] == "web" and web["is_web"]
    assert 20100 <= web["host_port"] <= 29999
    assert stack["endpoints"][0]["domain"] == "app.example.com"

    # Host: tenant provisioned, units written, service running.
    assert "hosty-t-1" in stack_host.users
    uid = stack_host.users["hosty-t-1"]
    assert any(name.endswith(".container") for name in stack_host.unit_files[uid])
    assert ("hosty-t-1", "blog-web.service") in stack_host.active
    assert ("hosty-t-1", "blog", "data") in stack_host.volume_dirs
    # Env reached an env file, never unit text.
    assert any("NODE_ENV=production" in content for content in stack_host.env_files.values())
    assert all("NODE_ENV" not in content for content in stack_host.unit_files[uid].values())
    # Published ingress routes the domain to the loopback port.
    assert any(
        "app.example.com" in str(cfg) and f"127.0.0.1:{web['host_port']}" in str(cfg)
        for cfg in stack_host.caddy.configs
    )


async def test_create_dynamic_postgres_template(admin_client, stack_host):
    body = {
        "name": "postgres-test",
        "blueprint_id": "postgres",
        "inputs": {"version": "16", "database": "postgres", "user": "postgres"},
    }
    payload, op = await create_stack_ok(admin_client, body)
    assert op["status"] == "succeeded", op
    assert payload["show_once"]["db_password"]

    stack = (await admin_client.get(f"/api/stacks/{payload['stack']['id']}")).json()
    service = stack["services"][0]
    assert service["name"] == "db"
    assert service["image"] == (
        "docker.io/library/postgres:16"
        "@sha256:081f1bc7bd5e143dbb6e487b710bbc27712cdcfaced4c071b8e47349aa1b4171"
    )
    assert service["internal_port"] == 5432
    assert service["host_port"] is not None
    assert stack["volumes"][0]["mount_path"] == "/var/lib/postgresql/data"


async def test_create_dynamic_mongodb_template(admin_client, stack_host):
    body = {
        "name": "mongodb-test",
        "blueprint_id": "mongodb",
        "inputs": {"version": "7.0", "username": "root"},
    }
    payload, op = await create_stack_ok(admin_client, body)
    assert op["status"] == "succeeded", op
    assert payload["show_once"]["root_password"]

    stack = (await admin_client.get(f"/api/stacks/{payload['stack']['id']}")).json()
    service = stack["services"][0]
    assert service["name"] == "db"
    assert service["image"] == (
        "docker.io/library/mongo:7.0"
        "@sha256:8ecb514b00bdcc0bde67ef4e6c330385377a9dc68e24ee94e28c07c891647348"
    )
    assert service["internal_port"] == 27017
    assert service["host_port"] is not None
    assert stack["volumes"][0]["mount_path"] == "/data/db"


async def test_create_dynamic_uptime_kuma_template(admin_client, stack_host):
    body = {
        "name": "uptime-kuma-test",
        "blueprint_id": "uptime-kuma",
        "inputs": {},
    }
    payload, op = await create_stack_ok(admin_client, body)
    assert op["status"] == "succeeded", op

    stack = (await admin_client.get(f"/api/stacks/{payload['stack']['id']}")).json()
    service = stack["services"][0]
    assert service["name"] == "web"
    assert service["image"] == (
        "docker.io/louislam/uptime-kuma:1"
        "@sha256:3d632903e6af34139a37f18055c4f1bfd9b7205ae1138f1e5e8940ddc1d176f9"
    )
    assert service["internal_port"] == 3001
    assert service["host_port"] is not None
    assert stack["volumes"][0]["mount_path"] == "/app/data"


async def test_create_git_template_persists_build_source(admin_client, stack_host):
    body = {
        "name": "git-test",
        "blueprint_id": "git",
        "inputs": {
            "repo": "https://github.com/example/app.git",
            "branch": "main",
            "internal_port": 3000,
            "domain": "git.example.com",
        },
    }
    payload, op = await create_stack_ok(admin_client, body)
    assert op["status"] == "succeeded", op

    stack = (await admin_client.get(f"/api/stacks/{payload['stack']['id']}")).json()
    service = stack["services"][0]
    assert service["internal_port"] == 3000
    assert service["host_port"] is not None
    assert service["is_web"] is True
    assert stack["endpoints"][0]["domain"] == "git.example.com"

    uid = stack_host.users["hosty-t-1"]
    build = stack_host.unit_files[uid]["git-test-web.build"]
    assert "SetWorkingDirectory=https://github.com/example/app.git#main" in build


async def test_dynamic_templates_allocate_distinct_ports(admin_client, stack_host):
    first = {
        "name": "postgres-test",
        "blueprint_id": "postgres",
        "inputs": {"version": "16", "database": "postgres", "user": "postgres"},
    }
    second = {
        "name": "mongodb-test",
        "blueprint_id": "mongodb",
        "inputs": {"version": "7.0", "username": "root"},
    }

    first_payload, _ = await create_stack_ok(admin_client, first)
    second_payload, _ = await create_stack_ok(admin_client, second)

    first_stack = (await admin_client.get(f"/api/stacks/{first_payload['stack']['id']}")).json()
    second_stack = (await admin_client.get(f"/api/stacks/{second_payload['stack']['id']}")).json()
    assert first_stack["services"][0]["host_port"] != second_stack["services"][0]["host_port"]


async def test_create_failure_degrades_then_next_cycle_heals(admin_client, stack_host, app):
    stack_host.fail_control.add("start")
    resp = await admin_client.post("/api/stacks", json=CREATE_BODY)
    assert resp.status_code == 202
    payload = resp.json()
    op = (await admin_client.get(f"/api/operations/{payload['operation_id']}")).json()
    assert op["status"] == "failed"
    assert any(s["status"] == "failed" for s in op["steps"])

    stack = (await admin_client.get(f"/api/stacks/{payload['stack']['id']}")).json()
    assert stack["status"] == "degraded"
    assert "start" in stack["error_message"]
    # No compensation: the written units stay; the NEXT cycle finishes the job.
    uid = stack_host.users["hosty-t-1"]
    assert stack_host.unit_files[uid]

    stack_host.fail_control.clear()
    await app.state.reconciler.converge_all()
    stack = (await admin_client.get(f"/api/stacks/{payload['stack']['id']}")).json()
    assert stack["status"] == "ready"
    assert ("hosty-t-1", "blog-web.service") in stack_host.active


async def test_create_validations(admin_client, stack_host):
    bad_blueprint = dict(CREATE_BODY, blueprint_id="no-such-blueprint")
    assert (await admin_client.post("/api/stacks", json=bad_blueprint)).status_code == 422

    bad_name = dict(CREATE_BODY, name="-leading-hyphen")
    assert (await admin_client.post("/api/stacks", json=bad_name)).status_code == 422

    bad_image = dict(CREATE_BODY, inputs=dict(CREATE_BODY["inputs"], image="--privileged"))
    assert (await admin_client.post("/api/stacks", json=bad_image)).status_code == 422

    await create_stack_ok(admin_client)
    dup_name = dict(CREATE_BODY, inputs=dict(CREATE_BODY["inputs"], domain="other.example.com"))
    assert (await admin_client.post("/api/stacks", json=dup_name)).status_code == 409
    dup_domain = dict(CREATE_BODY, name="blog2")
    assert (await admin_client.post("/api/stacks", json=dup_domain)).status_code == 409


async def test_delete_stack(admin_client, stack_host):
    payload, _ = await create_stack_ok(admin_client)
    stack_id = payload["stack"]["id"]

    resp = await admin_client.request(
        "DELETE", f"/api/stacks/{stack_id}", json={"confirm_name": "BLOG"}
    )
    assert resp.status_code == 202, resp.text
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded", op
    # Rows finalized away, host fully torn down, tenant released.
    assert (await admin_client.get(f"/api/stacks/{stack_id}")).status_code == 404
    assert not stack_host.active
    assert not stack_host.volume_dirs
    assert not any(stack_host.unit_files.values())
    assert "hosty-t-1" not in stack_host.users


async def test_delete_requires_matching_confirmation(admin_client, stack_host):
    payload, _ = await create_stack_ok(admin_client)
    resp = await admin_client.request(
        "DELETE", f"/api/stacks/{payload['stack']['id']}", json={"confirm_name": "nope"}
    )
    assert resp.status_code == 409


async def test_quota_enforced_for_clients(admin_client, stack_host):
    headers = await make_client_headers(admin_client, "tenant", max_stacks=1)
    resp = await admin_client.post("/api/stacks", json=CREATE_BODY, headers=headers)
    assert resp.status_code == 202, resp.text
    second = dict(
        CREATE_BODY,
        name="blog2",
        inputs=dict(CREATE_BODY["inputs"], domain="two.example.com"),
    )
    resp = await admin_client.post("/api/stacks", json=second, headers=headers)
    assert resp.status_code == 409
    assert "quota" in resp.json()["error"]["message"].lower()


async def test_clients_cannot_see_others_stacks(admin_client, stack_host):
    payload, _ = await create_stack_ok(admin_client)
    stack_id = payload["stack"]["id"]

    headers = await make_client_headers(admin_client, "peeker")
    assert (await admin_client.get("/api/stacks", headers=headers)).json() == []
    # 404, not 403 — existence must not leak.
    assert (await admin_client.get(f"/api/stacks/{stack_id}", headers=headers)).status_code == 404
    assert (
        await admin_client.get(f"/api/stacks/{stack_id}/logs", headers=headers)
    ).status_code == 404


async def test_owner_suspension_scales_to_zero(admin_client, stack_host, app):
    headers = await make_client_headers(admin_client, "sleepy")
    resp = await admin_client.post("/api/stacks", json=CREATE_BODY, headers=headers)
    assert resp.status_code == 202
    stack_id = resp.json()["stack"]["id"]
    users = (await admin_client.get("/api/users")).json()
    user_id = next(u["id"] for u in users if u["username"] == "sleepy")

    resp = await admin_client.patch(f"/api/users/{user_id}", json={"suspended": True})
    assert resp.status_code == 200
    await app.state.reconciler.converge_all()
    stack = (await admin_client.get(f"/api/stacks/{stack_id}")).json()
    assert stack["status"] == "suspended"
    assert not stack_host.active  # scaled to zero, volumes kept
    assert stack_host.volume_dirs

    resp = await admin_client.patch(f"/api/users/{user_id}", json={"suspended": False})
    assert resp.status_code == 200
    await app.state.reconciler.converge_all()
    stack = (await admin_client.get(f"/api/stacks/{stack_id}")).json()
    assert stack["status"] == "ready"


async def test_logs(admin_client, stack_host, monkeypatch):
    payload, _ = await create_stack_ok(admin_client)
    stack_id = payload["stack"]["id"]
    calls: list[tuple] = []

    async def journal(user, unit, *, tail=200):
        calls.append((user, unit, tail))
        return f"journal of {unit}"

    monkeypatch.setattr(systemd_user_mod, "journal", journal)
    resp = await admin_client.get(f"/api/stacks/{stack_id}/logs?tail=50")
    assert resp.status_code == 200
    assert resp.json() == {"service": "web", "logs": "journal of blog-web.service"}
    assert calls == [("hosty-t-1", "blog-web.service", 50)]

    assert (await admin_client.get(f"/api/stacks/{stack_id}/logs?service=nope")).status_code == 404


# --- action dispatch ----------------------------------------------------------------


class EchoInputs(BaseModel):
    image: str = Field(default="ghcr.io/org/echo:v1")
    internal_port: int = Field(default=8080)


class EchoBlueprint:
    """Minimal blueprint exercising actions + generated show-once secrets."""

    id = "echo"
    version = 1

    def inputs(self) -> type[EchoInputs]:
        return EchoInputs

    def ports_needed(self, inputs: EchoInputs) -> list[str]:
        return ["web"]

    def secrets_needed(self, inputs: EchoInputs) -> list[str]:
        return ["admin_password"]

    def render(self, name: str, inputs: EchoInputs, alloc: Allocation) -> StackSpec:
        service = ServiceSpec(
            name="web",
            image=inputs.image,
            env=(("ADMIN_PASSWORD", alloc.secrets["admin_password"]),),
            internal_port=inputs.internal_port,
            host_port=alloc.ports["web"],
            is_web=True,
        )
        return StackSpec(name=name, tenant=alloc.tenant, services=(service,))

    def actions(self):
        async def ping(*, db, settings, stack: Stack, inputs, params) -> ActionResult:
            return ActionResult(
                ok=True,
                message=f"pong from {stack.name}",
                data={"blueprint": stack.blueprint_id},
                show_once={"token": "one-time-token"},
            )

        async def explode(*, db, settings, stack: Stack, inputs, params) -> ActionResult:
            return ActionResult(ok=False, message="boom")

        return {"ping": ping, "explode": explode}

    def backup_hooks(self) -> None:
        return None

    def health(self, observed_active: dict[str, bool]) -> StackHealth:
        return StackHealth(healthy=bool(observed_active.get("web")))


@pytest.fixture
def echo_blueprint():
    blueprints_base.register(EchoBlueprint())
    yield
    blueprints_base._REGISTRY.pop("echo", None)


async def test_action_dispatch_and_show_once(admin_client, stack_host, echo_blueprint):
    body = {"name": "echo1", "blueprint_id": "echo", "inputs": {}}
    payload, op = await create_stack_ok(admin_client, body)
    assert op["status"] == "succeeded", op
    # Generated secret surfaces exactly once, at create.
    assert payload["show_once"]["admin_password"]
    stack_id = payload["stack"]["id"]

    resp = await admin_client.post(f"/api/stacks/{stack_id}/actions/ping")
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "message": "pong from echo1",
        "data": {"blueprint": "echo"},
        "show_once": {"token": "one-time-token"},
    }

    resp = await admin_client.post(f"/api/stacks/{stack_id}/actions/explode")
    assert resp.status_code == 200 and resp.json()["ok"] is False

    assert (await admin_client.post(f"/api/stacks/{stack_id}/actions/missing")).status_code == 404


# --- auto-generated + editable domains -------------------------------------------------


async def test_suggested_domain_base_then_sslip_then_none(admin_client, stack_host, settings):
    settings.apps_base_domain = "apps.example.com"
    r = await admin_client.get("/api/stacks/suggested-domain", params={"name": "blog"})
    assert r.status_code == 200 and r.json()["domain"] == "blog.apps.example.com"

    # No wildcard base configured → sslip.io off the public IP (zero DNS setup).
    settings.apps_base_domain = None
    settings.public_ip = "203.0.113.9"
    r = await admin_client.get("/api/stacks/suggested-domain", params={"name": "blog"})
    assert r.json()["domain"] == "blog.203.0.113.9.sslip.io"

    # Neither configured → nothing to generate.
    settings.public_ip = ""
    r = await admin_client.get("/api/stacks/suggested-domain", params={"name": "blog"})
    assert r.status_code == 409


async def test_create_auto_assigns_domain_when_blank(admin_client, stack_host, settings):
    settings.apps_base_domain = "apps.example.com"
    body = {
        "name": "autoweb",
        "blueprint_id": "raw-image",
        "inputs": {"image": "nginx:1.27", "internal_port": 80},
    }
    payload, op = await create_stack_ok(admin_client, body)
    assert op["status"] == "succeeded", op
    assert [e["domain"] for e in payload["stack"]["endpoints"]] == ["autoweb.apps.example.com"]


async def test_create_keeps_explicit_domain(admin_client, stack_host, settings):
    settings.apps_base_domain = "apps.example.com"  # ignored when a domain is given
    payload, _ = await create_stack_ok(admin_client)  # CREATE_BODY → app.example.com
    assert [e["domain"] for e in payload["stack"]["endpoints"]] == ["app.example.com"]


async def test_db_stack_gets_no_auto_domain(admin_client, stack_host, settings):
    settings.apps_base_domain = "apps.example.com"
    body = {
        "name": "pgonly",
        "blueprint_id": "postgres",
        "inputs": {"version": "16", "database": "app", "user": "app"},
    }
    payload, _ = await create_stack_ok(admin_client, body)
    assert payload["stack"]["endpoints"] == []  # no web service → internal only


async def test_set_stack_domain_changes_and_reconverges(admin_client, stack_host):
    payload, _ = await create_stack_ok(admin_client)
    stack_id = payload["stack"]["id"]
    r = await admin_client.put(f"/api/stacks/{stack_id}/domain", json={"domain": "new.example.com"})
    assert r.status_code == 202, r.text
    detail = (await admin_client.get(f"/api/stacks/{stack_id}")).json()
    assert [e["domain"] for e in detail["endpoints"]] == ["new.example.com"]
    assert any("new.example.com" in str(c) for c in stack_host.caddy.configs)


async def test_set_stack_domain_blank_autogenerates(admin_client, stack_host, settings):
    settings.apps_base_domain = "apps.example.com"
    payload, _ = await create_stack_ok(admin_client)
    stack_id = payload["stack"]["id"]
    r = await admin_client.put(f"/api/stacks/{stack_id}/domain", json={})
    assert r.status_code == 202, r.text
    detail = (await admin_client.get(f"/api/stacks/{stack_id}")).json()
    assert [e["domain"] for e in detail["endpoints"]] == ["blog.apps.example.com"]


async def test_set_stack_domain_rejected_for_non_web_stack(admin_client, stack_host):
    body = {
        "name": "pgonly",
        "blueprint_id": "postgres",
        "inputs": {"version": "16", "database": "app", "user": "app"},
    }
    payload, _ = await create_stack_ok(admin_client, body)
    stack_id = payload["stack"]["id"]
    r = await admin_client.put(f"/api/stacks/{stack_id}/domain", json={"domain": "pg.example.com"})
    assert r.status_code == 422


async def test_set_stack_domain_refuses_conflict(admin_client, stack_host):
    await create_stack_ok(admin_client)  # occupies app.example.com
    other, _ = await create_stack_ok(
        admin_client,
        {
            "name": "second",
            "blueprint_id": "raw-image",
            "inputs": {"image": "nginx:1.27", "internal_port": 80, "domain": "second.example.com"},
        },
    )
    # Point the second stack at the first's domain → conflict.
    r = await admin_client.put(
        f"/api/stacks/{other['stack']['id']}/domain", json={"domain": "app.example.com"}
    )
    assert r.status_code == 409
