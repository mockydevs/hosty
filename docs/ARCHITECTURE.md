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

### ADR-008: Filebrowser with proxy auth, scoped per site, behind the panel

**Status:** accepted (Week 16)

One Filebrowser instance on an internal-only listener (`127.0.0.1:8082`),
`auth.method=proxy`: it trusts the `X-Hosty-Fb-User` header. Only the panel's
`/files` reverse proxy can reach it, and the proxy (a) strips any
client-supplied copy of that header and (b) injects the site user extracted
from a SIGNED session token — so which files a session sees is decided by the
panel's HMAC signature, not by anything the browser sends. Each site gets a
Filebrowser user whose scope is locked to its own directory; Filebrowser
enforces the directory boundary, the panel enforces identity.

**Revised after first VM run (Week 16):** user management goes through
Filebrowser's REST API (panel authenticates as a provision-time `admin` user
via the same proxy header → JWT), NOT the CLI — the daemon holds an exclusive
BoltDB lock, so `filebrowser users add` against the live database deadlocks.
Also discovered: proxy auth AUTO-CREATES unknown users with the global
default scope, which would have exposed the whole sites root. The default
scope is therefore pinned to an empty quarantine directory
(`/.hosty-quarantine`) at provision time.

**Known limitation (verify on VM):** Filebrowser runs as root, so files it
creates are root-owned until ownership normalization (event-hook `chown`) is
configured — tracked as an open Phase 6 item.

### ADR-009: Backend runs inside the dev VM, not over SSH

**Status:** accepted (Week 2 decision, implemented Week 16)

The Week 2 roadmap left open whether the backend connects to the dev VM over
SSH or runs inside it. **Decision: inside.** The system layer
(`system/runner.py`) execs commands on the local machine — exactly how
production works (root systemd service, ADR-002). An SSH transport would add
a remote-execution abstraction that production doesn't have and that every
system-layer test would then have to fake.

Mechanics (`installer/dev-vm/`): Multipass VM `hosty-dev` (Ubuntu 24.04),
repo mounted at `/home/ubuntu/hosty` so host edits are live inside the VM.
Provisioning is the same `installer/provision.sh` production will use, plus
`vm-setup.sh` for backend deps. **Stateful things live outside the mount**
(`/var/lib/hosty`: venv, SQLite DB, dev.env) — Multipass mounts don't support
SQLite's file locking and make venvs slow. The frontend dev server stays on
the host and proxies `/api`, `/files`, `/adminer` to the VM IP
(`HOSTY_API_TARGET`). Rejected: WSL2 (not production-like enough — no real
systemd boot, different networking) and a cloud VM as the default (costs
money, needs credentials; still the right choice for installer testing in
Phase 10).

### ADR-010: Backups — local-first with optional S3 mirror; in-process scheduler

**Status:** accepted (Phase 8)

- A backup is a directory: `files.tar.zst` + one `mysqldump` per database +
  `manifest.json` (sha256 checksums, written last — no manifest means
  incomplete, ignored, eventually pruned). Restores verify every checksum
  before touching anything.
- Local retention (keep newest N per site) prunes only local copies; the
  panel NEVER deletes S3 objects.
- S3 (MinIO-compatible) credentials are managed in the UI and stored in the
  panel DB with the secret key encrypted at rest (Fernet keyed from
  HOSTY_SECRET_KEY — see `app/core/secrets.py`). Credentials are verified
  against the bucket before being saved. `HOSTY_S3_*` env vars remain as a
  bootstrap fallback only.
- What each backup contains is per-site configuration (files / databases /
  S3 mirror), honored by both run-now and the scheduler.
- Scheduler is a 60-second asyncio tick inside the panel process (no
  APScheduler/cron dependency): daily at HH:00 or weekly (Monday), serialized
  by a global lock so only one backup/restore runs at a time.
- An S3 mirror failure marks the operation failed but keeps the durable local
  backup; the error message says exactly that.
- Restores have no rollback by design (they overwrite in place); scope can be
  full, files-only, or databases-only, fetched from S3 when not local.
