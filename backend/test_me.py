import time
import jwt
from httpx import Client

# Fetch secret key
env_dict = {}
with open(".env", "r") as f:
    for line in f:
        if "=" in line:
            k, v = line.strip().split("=", 1)
            env_dict[k] = v.strip("'\"")

secret = env_dict.get("HOSTY_SECRET_KEY", "no_secret")

now = int(time.time())
token = jwt.encode(
    {"sub": "1", "ver": 1, "exp": now + 3600, "type": "access"},
    secret,
    algorithm="HS256"
)

client = Client()
resp = client.get(
    "http://127.0.0.1:8801/api/auth/me",
    headers={"Authorization": f"Bearer {token}"}
)
print("Auth/me status:", resp.status_code)
print("Auth/me text:", resp.text)

resp2 = client.get(
    "http://127.0.0.1:8801/api/stacks",
    headers={"Authorization": f"Bearer {token}"}
)
print("Stacks status:", resp2.status_code)
print("Stacks text:", resp2.text)
