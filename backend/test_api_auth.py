import time
import jwt as pyjwt
import httpx
import sys

# Assume secret_key is "testing-secret-key" or something, but we need the actual one
# Let's read it from backend/.env
env_dict = {}
with open(".env", "r") as f:
    for line in f:
        if "=" in line:
            k, v = line.strip().split("=", 1)
            env_dict[k] = v.strip("'\"")

secret = env_dict.get("HOSTY_SECRET_KEY", "")

now = int(time.time())
token = pyjwt.encode(
    {"sub": "1", "ver": 1, "exp": now + 3600},
    secret,
    algorithm="HS256"
)

resp = httpx.get(
    "http://127.0.0.1:8801/api/stacks",
    headers={"Authorization": f"Bearer {token}"}
)
print(resp.status_code)
print(resp.text)
