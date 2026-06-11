# Hosty Architecture

```
React UI (Tailwind/shadcn)  ← Phase 2
        ↓ REST (OpenAPI)
FastAPI backend (this repo: backend/)
        ↓ app/system/* (single shell boundary)
Caddy · PHP-FPM · MariaDB · PowerDNS · systemd
        Ubuntu 24.04 LTS
```

Backend layering (dependencies point downward only):

```
api/routes   → HTTP, validation, status codes. No business logic.
services/    → business logic, orchestration, transactions. (from Phase 3)
system/      → the ONLY place that executes shell commands.
db/          → models + engine/session. core/ → config, security, errors.
```

---

## Architecture Decision Records

### ADR-001: Caddy is managed via its Admin API (JSON config), not Caddyfiles

**Status:** accepted (Week 1) · **Context:** we must create/update/delete vhosts
programmatically and atomically.

**Decision:** the panel holds the desired state in its own DB and syncs the *full*
Caddy JSON config through the Admin API (`POST /load`), rather than patching
fragments or writing Caddyfile snippets to disk.

**Consequences:** config generation is a pure, snapshot-testable function
(models in → JSON out); validation failures leave the previous config running;
no file-watching or reload races. Trade-off: raw JSON is less human-readable
than a Caddyfile — mitigated by an export/debug endpoint later.

### ADR-002: The panel service runs as root for v1

**Status:** accepted (Week 1) · **Context:** vhost/user/database provisioning
requires root-level operations (useradd, systemctl, writing pool configs).

**Decision:** v1 runs as a root systemd service, like most existing panels.
Risk is contained by structure, not privilege separation:
every shell call goes through `app/system/runner.py` (argv lists only, no
`shell=True` — enforced by a CI grep guard), all inputs are validated with
strict allowlists, and site users can only ever match `site-*` so the panel
cannot touch system accounts.

**Revisit:** post-1.0, move to a dedicated `hosty` user with a sudoers
whitelist per command.

### ADR-003: SQLite (WAL mode) for the panel database

**Status:** accepted · single-admin panel, low write volume; WAL gives
concurrent reads; zero extra services. Alembic manages the schema from day one
so a later PostgreSQL move is a connection-string change plus a migration run.

### ADR-004: Auth = short-lived JWT access tokens + rotating refresh cookie

**Status:** accepted (Week 4)

- Access tokens: HS256 JWT, 15 min, sent as `Authorization: Bearer`.
- Refresh tokens: opaque 384-bit random values, stored **hashed** (SHA-256) in
  the DB, delivered as an `httpOnly` `SameSite=Strict` cookie scoped to
  `/api/auth`, rotated on every refresh.
- Reuse of a rotated refresh token revokes the user's whole session family
  (stolen-token detection).
- Password change revokes all refresh tokens and invalidates previously issued
  access tokens (`iat` < password_changed_at).
- Passwords: Argon2id. Login rate-limited per client IP (sliding window).
- First boot: `/api/auth/setup` is open only while zero users exist, then locks.

### ADR-005: All system mutations behind one runner

**Status:** accepted (Week 5) · `app/system/runner.py` is the single subprocess
entrypoint: argv-only execution (shell injection impossible by construction),
timeouts, structured logging of every command, typed results. Command *builders*
are pure functions unit-tested for exact argv output, including injection
attempts. Higher layers never import `subprocess`/`asyncio.subprocess` directly
(CI-enforced).

### ADR-006: Dev runtime is Python 3.10+, production is 3.12

**Status:** accepted · code avoids 3.11+-only APIs (e.g. `datetime.UTC`) so it
runs on constrained dev sandboxes; CI and production pin 3.12.

### ADR-007: MariaDB via validated-identifier CLI; Adminer behind a panel proxy

**Status:** accepted (Week 14-15)

**Database SQL:** the panel executes MariaDB statements through the `mariadb`
CLI (root unix socket) rather than a driver. True parameter binding is not
available that way, but none of the inputs are free-form: database/user names
must match `^[a-z][a-z0-9_]{1,63}$` (validated twice — Pydantic and the
service layer) and passwords are panel-generated hex tokens, validated as
such before being embedded. Identifiers cannot be parameterized even with a
driver. **Revisit** if databases ever accept arbitrary external input
(e.g. import tooling): switch to a driver + bound parameters then.

**Credentials:** shown exactly once (creation / reset-password response);
only a SHA-256 hash is stored in the panel DB.

**Adminer:** served by Caddy on an internal-only listener
(`127.0.0.1:8081`) through a dedicated low-privilege PHP-FPM pool
(`www-data`, `open_basedir` locked to the Adminer directory), and reached
exclusively via the panel's `/adminer` reverse proxy. Access control: the
panel mints a short-lived HMAC ticket (60 s) for the logged-in admin; the
proxy exchanges it for a signed session cookie (30 min, `Path=/adminer`,
`HttpOnly`). **Tradeoff:** credential auto-fill into Adminer's login form is
deferred — it would require shipping an Adminer login plugin; users paste the
password from the show-once dialog instead.
