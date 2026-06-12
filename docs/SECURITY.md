# Hosty Security Model (Week 22 threat-model pass)

Reviewed: 2026-06-12 · every endpoint checked for **authz**, **input
validation**, and **audit logging**. Re-review whenever a router is added
(CI cannot catch a forgotten `Depends(get_current_user)` — humans must).

## Trust boundaries

```
Internet ── Caddy (:443, panel vhost, optional IP allowlist)
                │ reverse_proxy
            Panel API (uvicorn 127.0.0.1:8800, root — ADR-002)
                │ app/system/* (argv-only runner, ADR-005)
            systemd · useradd · mariadb CLI · WP-CLI (runuser, never root)
                │ internal-only listeners
            Adminer (127.0.0.1:8081) · Filebrowser (127.0.0.1:8082)
```

- The panel process is the only thing that crosses from HTTP to the system.
- Adminer/Filebrowser are reachable **only** through the panel's
  authenticated proxies (HMAC tickets → scoped session cookies); their
  listeners bind to localhost.
- Defense-in-depth: `panel_allowed_ips` is enforced both at Caddy
  (remote_ip matcher) and in the app (IPAllowlistMiddleware).

## Endpoint inventory

Authz column: `admin` = `Depends(get_current_user)` (JWT, iat checked against
password changes). Audit: all mutating (POST/PUT/PATCH/DELETE) `/api/*`
requests are recorded by AuditLogMiddleware (user, method, path, status, IP —
**never bodies**, they can contain passwords). `/api/auth/refresh` is excluded
as rotation noise.

| Endpoint | Authz | Validation | Notes |
|---|---|---|---|
| `GET /api/health` | public | — | liveness only, no data |
| `POST /api/auth/setup` | open while 0 users, then 409 | Pydantic (username regex, password 12-128) | audited |
| `POST /api/auth/login` | rate-limited per IP | Pydantic | audited incl. failures |
| `POST /api/auth/refresh` | refresh cookie (rotated, reuse-detected) | — | not audited (noise) |
| `POST /api/auth/logout`, `change-password` | admin / cookie | Pydantic | audited; change revokes all sessions |
| `GET /api/system/*`, `POST .../actions/{action}` | admin | unit ∈ managed allowlist, action ∈ CONTROL_ACTIONS | audited |
| `/api/sites` CRUD | admin | domain (IDNA+regex), PHP version allowlist, delete requires typed confirm | audited; pipeline compensations on failure |
| `/api/sites/{id}/php*` | admin | version allowlist, sizes `^[1-9][0-9]{0,3}M$` | audited |
| `/api/sites/{id}/wordpress*` | admin | title/user/email/locale/version validated; WP-CLI via runuser | audited |
| `/api/databases*` | admin | identifiers `^[a-z][a-z0-9_]{1,63}$` (validated twice), passwords generated-only (ADR-007) | audited; creds shown once, stored hashed |
| `/api/dns*` | admin | per-record-type Pydantic models | audited |
| `/api/backups*`, `/api/sites/{id}/backups` | admin | site ownership checked; S3 creds encrypted at rest | audited |
| `/api/operations/{id}`, `/api/audit` | admin | paging bounds | read-only |
| `/adminer/*` proxy | HMAC ticket (60s) → cookie (30m, Path=/adminer) | scope signature-bound | identity decided by panel, not client |
| `/files/*` proxy | HMAC ticket → cookie (Path=/files), scope carries site user | spoofed `X-Hosty-Fb-User` stripped; Filebrowser enforces directory scope |

## Standing invariants (CI-enforced, `scripts/forbidden_patterns.sh`)

1. No `shell=True` anywhere; subprocess confined to `app/system/runner.py`.
2. SQL statement strings only in `app/services/mariadb.py` (validated
   identifiers + generated secrets; ADR-007 revisit condition documented).
3. WP-CLI argv has two approved owners during the v1-to-v2 transition:
   `app/services/wordpress.py` executes via `runuser` as the site user;
   `app/orchestration/blueprints/wordpress.py` executes through the rootless
   per-tenant Podman adapter. M6 removes the legacy owner.
4. Audit middleware never reads request bodies.

## Known gaps / accepted risks (v1)

- Panel runs as root (ADR-002); revisit post-1.0 with a sudoers whitelist.
- Filebrowser runs as root; uploaded-file ownership normalization pending VM
  verification (Phase 6 note).
- OWASP ZAP baseline scan and the full E2E security pass require the running
  VM — scheduled for the VM testing round.
- Single admin user; roles/2FA are post-1.0 backlog.
