import asyncio
import os
import json
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.main import app
from app.api.deps import get_current_user, get_db
from app.db.models import User, Stack
from app.core.secrets import encrypt_secret
from sqlalchemy import select

engine = create_async_engine("sqlite+aiosqlite:///hosty.db")
async_session = async_sessionmaker(engine, expire_on_commit=False)

async def override_get_db():
    async with async_session() as session:
        yield session

async def override_get_current_user():
    return User(id=1, email="test@test.com", role="admin")

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_user] = override_get_current_user

# Get secret key from backend/.env.example and mock app.state.settings.secret_key
env_dict = {}
with open(".env.example", "r") as f:
    for line in f:
        if "=" in line:
            k, v = line.strip().split("=", 1)
            env_dict[k] = v.strip("'\"")

secret_key = env_dict.get("HOSTY_SECRET_KEY", "example_secret")

# Wait, `request.app.state.settings` is already initialized in `app.main` with empty/missing?
# Fastapi testclient might use the existing settings. 
# We can overwrite it:
import app.core.config
app.state.settings = app.core.config.Settings(secret_key=secret_key, database_url="sqlite+aiosqlite:///hosty.db")

async def add_stack():
    async with async_session() as db:
        res = await db.execute(select(Stack).where(Stack.id == 1))
        stack = res.scalar_one_or_none()
        if not stack:
            enc = encrypt_secret(json.dumps({}), secret_key)
            new_stack = Stack(id=1, name="test-stack", blueprint_id="python", blueprint_version=1, inputs_encrypted=enc, status="ready", generation=1, observed_generation=1, owner_id=1)
            db.add(new_stack)
            await db.commit()

asyncio.run(add_stack())

client = TestClient(app)
try:
    resp = client.get("/api/stacks")
    print("Stacks status:", resp.status_code)
    print("Stacks text:", resp.text)
except Exception as e:
    import traceback
    traceback.print_exc()

