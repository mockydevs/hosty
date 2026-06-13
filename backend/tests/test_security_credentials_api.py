from __future__ import annotations

from app.db.models import SshKey
from tests.conftest import TEST_USER

PRIVATE_KEY = """-----BEGIN OPENSSH PRIVATE KEY-----
test-private-key
-----END OPENSSH PRIVATE KEY-----
"""
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest test@example.com"


async def test_security_credentials_empty_state(admin_client):
    keys = await admin_client.get("/api/security/keys")
    assert keys.status_code == 200, keys.text
    assert keys.json() == []

    tokens = await admin_client.get("/api/security/tokens")
    assert tokens.status_code == 200, tokens.text
    assert tokens.json() == []


async def test_ssh_key_create_list_delete(admin_client, app):
    created = await admin_client.post(
        "/api/security/keys",
        json={"name": "github-main", "private_key": PRIVATE_KEY, "public_key": PUBLIC_KEY},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "github-main"
    assert body["public_key"] == PUBLIC_KEY
    assert "private" not in created.text.lower()

    listed = await admin_client.get("/api/security/keys")
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "github-main"

    async with app.state.sessionmaker() as db:
        stored = await db.get(SshKey, body["id"])
        assert stored is not None
        assert stored.private_key_encrypted != PRIVATE_KEY

    duplicate = await admin_client.post(
        "/api/security/keys",
        json={"name": "github-main", "private_key": PRIVATE_KEY, "public_key": PUBLIC_KEY},
    )
    assert duplicate.status_code == 409

    deleted = await admin_client.delete(f"/api/security/keys/{body['id']}")
    assert deleted.status_code == 200
    assert (await admin_client.get("/api/security/keys")).json() == []


async def test_api_token_auth_and_revoke(admin_client):
    created = await admin_client.post("/api/security/tokens", json={"name": "deploy"})
    assert created.status_code == 200, created.text
    body = created.json()
    raw_token = body["token"]
    assert raw_token.startswith("hst_")

    me = await admin_client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
    assert me.status_code == 200, me.text
    assert me.json()["username"] == TEST_USER

    listed = await admin_client.get("/api/security/tokens")
    assert listed.status_code == 200
    token_row = listed.json()[0]
    assert token_row["name"] == "deploy"
    assert token_row["last_used_at"] is not None

    deleted = await admin_client.delete(f"/api/security/tokens/{body['id']}")
    assert deleted.status_code == 200

    rejected = await admin_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"}
    )
    assert rejected.status_code == 401
