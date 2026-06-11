"""Short-lived HMAC tokens for browser hand-offs (Adminer, Filebrowser).

`<scope>.<expiry>.<hmac>` — stateless, self-expiring, scope is covered by the
signature so it cannot be swapped. Scopes may carry data (e.g. a site user):
use `token_scope()` to extract it after signature verification.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(*, secret: str, ttl_seconds: int, scope: str = "session") -> str:
    if "." in scope:
        raise ValueError("Token scope must not contain '.'")
    expiry = str(int(time.time()) + ttl_seconds)
    payload = f"{scope}.{expiry}"
    return f"{payload}.{_sign(payload, secret)}"


def verify_token(token: str, *, secret: str, scope: str = "session") -> bool:
    parts = token.split(".")
    if len(parts) != 3:
        return False
    token_scope, expiry, signature = parts
    if token_scope != scope or not expiry.isdigit():
        return False
    payload = f"{token_scope}.{expiry}"
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return False
    return int(expiry) >= time.time()


def token_scope(token: str, *, secret: str) -> str | None:
    """Return the (signature-verified, unexpired) scope of a token, else None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    token_scope_, expiry, _ = parts
    if not expiry.isdigit():
        return None
    return token_scope_ if verify_token(token, secret=secret, scope=token_scope_) else None
