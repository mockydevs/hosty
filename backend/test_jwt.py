import time
import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

private_key_obj = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
)
private_key = private_key_obj.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
).decode("utf-8")

now = int(time.time())
try:
    app_jwt = pyjwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str("12345")},
        private_key,
        algorithm="RS256",
    )
    print("Success:", app_jwt)
except Exception as e:
    import traceback
    traceback.print_exc()
