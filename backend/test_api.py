import asyncio
from fastapi.testclient import TestClient
from app.main import app
from app.api.deps import get_current_user
from app.db.database import SessionLocal
from app.db.models import User
from sqlalchemy import select

async def override_get_current_user():
    async with SessionLocal() as db:
        user = (await db.execute(select(User))).scalars().first()
        return user

app.dependency_overrides[get_current_user] = override_get_current_user

with TestClient(app) as client:
    print("GET /api/stacks")
    response = client.get("/api/stacks")
    print(response.status_code)
    print(response.text)

    print("GET /api/sources/1/repos")
    response = client.get("/api/sources/1/repos")
    print(response.status_code)
    print(response.text)
