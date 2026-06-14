import re

with open("backend/app/api/routes/sources.py", "r", encoding="utf-8") as f:
    content = f.read()

# Replace "iss": source.app_id with "iss": str(source.app_id)
# Also "iss": payload.get("app_id") if that exists. Let's just do a blind regex for source.app_id inside "iss":
content = re.sub(r'"iss"\s*:\s*source\.app_id', r'"iss": str(source.app_id)', content)

with open("backend/app/api/routes/sources.py", "w", encoding="utf-8") as f:
    f.write(content)
