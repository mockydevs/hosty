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

### ADR-011: Containerized apps are a new SITE TYPE, not a pivot to a general PaaS

**Status:** accepted — 12a runtime decisions locked (2026-06-12); compose
deferred to 12c (single-container apps + managed data services cover the
common Django/Next.js shapes without a compose policy engine)

Hosty will host containerized apps (Django, Next.js, Postgres, …) alongside
PHP/WordPress sites — as an **extension of the existing model, explicitly not
a rebuild of Coolify/Dokploy/CapRover**.

Why extend rather than pivot: Hosty's differentiation is the
hosting-business layer that developer-PaaS tools lack — client accounts,
quotas and plans, suspension that 503s sites, impersonation, per-client usage
metering, DNS/Cloudflare management, one-click WordPress. The container
runtime slots UNDER that layer; the layer itself is the product
("Coolify for hosting businesses"). Competing head-on with Coolify at
git-push-deploy is a losing race at this project's pace, and running Coolify
NEXT TO Hosty doesn't work either: two stacks fight over ports 80/443
(observed in production — Docker's iptables DNAT silently hijacks external
traffic even when Caddy holds the host sockets).

Decisions:

- **One proxy.** Caddy remains the only thing on 80/443. App containers are
  reached via `reverse_proxy` to `127.0.0.1:<published_port>` or a Docker
  network address — same vhost pipeline, TLS, and suspension semantics as
  PHP sites. A second proxy (Traefik et al.) is rejected outright.
- **Docker via the system layer, argv only.** All engine calls go through
  `system/docker.py` as pure, unit-tested argv builders executed by
  `runner.run` — the Docker SDK is rejected (new socket-client dependency,
  bypasses the one-seam audit, and the runner already exists). The Docker
  socket is root-equivalent: it is never exposed to tenants, and tenant
  workloads run with user namespaces / no privileged containers.
- **12a runtime invariants** (encoded in the argv builders, not left to
  callers): ports publish to `127.0.0.1:<panel-allocated high port>` only —
  a bare `-p` DNATs around ufw and re-creates the observed 80/443 hijack;
  every app gets its own bridge network (default-bridge cross-tenant traffic
  is the quiet lateral-movement path); `--security-opt no-new-privileges`
  always; container logs are json-file with hard `max-size`/`max-file` caps
  (a crash-looping tenant container must not fill the host disk); env vars
  travel via a root-only `--env-file` under `/var/lib/hosty/apps/<id>/`,
  never `-e` argv (visible in /proc and process listings); images are
  resolved tag→digest at deploy and the digest is what runs (a registry
  re-tag cannot silently change a tenant's running code); everything the
  panel creates carries `hosty.managed` + `hosty.app-id` labels, and
  reconciliation/GC only ever touches labeled objects.
- **Volumes are bind directories** under `/var/lib/hosty/apps/<id>/volumes/`,
  not named Docker volumes — the existing backup engine (ADR-010) and
  per-client disk metering already understand directories.
- **Deploy modes in cost order.** 12a: prebuilt image (registry pull) and
  `compose.yaml` (one app = one compose project, validated against a safety
  policy — no privileged/host-network/host-mounts/80-443 publishing; the
  panel owns networks and volumes). 12c: `docker build` from a git repo or
  tarball — cheap because the PROJECT ships the build recipe (Dockerfile).
  Buildpacks/nixpacks ("build without a Dockerfile") is deliberately last
  and optional — it is the endless-maintenance half of a PaaS.
- **Apps inherit tenancy.** `apps.owner_id`, quota checks (per-plan
  max_apps / memory / CPU via the existing limits model), usage metering
  (image+volume disk counted per client), suspension, audit — all reuse
  Phase 11 machinery. Container resource caps map to the same per-client
  limits that drive systemd slices for PHP sites.
- **Backups stay one engine.** App volumes and managed-database dumps join
  the existing backup directory format (ADR-010) rather than a parallel
  system.

Rejected: pivoting the whole panel to containers in one step — superseded in
DIRECTION by ADR-012 (container-first, WordPress included), but the rejection
of a big-bang rewrite stands; exposing raw Docker/compose to clients
(admin-curated at first); Kubernetes (wrong weight class for single-VPS
hosting businesses).

### ADR-012: Container-first — everything converges on the container runtime, including WordPress

**Status:** accepted (2026-06-12)

The product direction is that the container runtime built in Phase 12a is the
ONE way Hosty runs workloads long-term: Django, Next.js, databases — and
WordPress. What ADR-011 rejected was a big-bang rewrite; what this ADR
commits to is the same destination reached incrementally, with the working
native pipeline kept alive until containerized WordPress has feature parity
and a tested migration path.

Decisions:

- **The Sites API/UI is the stable contract; the runtime is an
  implementation detail.** `sites` gains a `runtime` column
  (`native | container`). Clients never choose a runtime — admins (and
  eventually the default) do. Every existing feature (WordPress install,
  updates, salts, login links, staging, import, backups, Filebrowser, usage,
  suspension) keeps its API shape regardless of runtime.
- **Containerized WordPress composition (Phase 13a):** official
  `wordpress:<php>-apache` image per supported PHP version (the apache
  variant first — one container, plain `reverse_proxy`, boring and
  debuggable; the fpm-variant optimization can come later), `wp-content` as
  a bind volume under the app directory layout, the EXISTING shared host
  MariaDB reached via the Docker host gateway (per-site database containers
  are rejected for density: ~30 WP sites on a 4 GB VPS cannot carry 30
  MariaDB processes), WP-CLI via `docker exec` behind the same
  `system/docker.py` seam.
- **Migration, not flag day (13b/13c):** a per-site native→container
  migration command (wp-content rsync into the volume, same database, vhost
  swap, rollback by swapping back), batch tooling, THEN the default flips
  for new sites. Native provisioning code is removed only after the last
  native site migrates — in a major release.
- **Density is the honest cost.** Native FPM pools with `pm=ondemand` idle
  near zero RAM; a per-site apache container idles at ~50-120 MB. The
  per-site Linux-user + slice model is denser today. Container-first is
  still right because: one mental model and one hardening surface instead
  of two; per-site PHP version freedom without ondrej-PPA coupling; true
  filesystem isolation (a WP RCE no longer shares a kernel-visible /var/www
  with every neighbor); portability of a site = image digest + volume + DB
  dump. The density gap is priced into plans, not hidden.

Rejected: rewriting `services/sites.py` in place (parallel runtime, then
migrate); per-site database containers (density); the `wordpress:fpm` image
for v1 (Caddy fastcgi into a container whose document root differs from the
host path is a debugging trap; apache variant first).

> **Superseded by ADR-013.** ADR-011's runtime decisions (root Docker, argv
> seam) and ADR-012's phased-parity plan were written under production
> constraints (cost, density, migration safety) that the owner has since
> lifted: this is a quality-first build with no legacy obligation. ADR-013
> replaces both with a clean container-native core. The 80/443 lesson, the
> one-proxy rule, loopback-only publishing, and the labels/digest/env-file
> invariants all carry forward unchanged.

### ADR-013: v2 — container-native core: Stacks, Blueprints, a pure-planner reconciler, rootless Podman per tenant

**Status:** accepted (2026-06-12). Supersedes ADR-011/012 runtime decisions;
preserves their security invariants.

**The diagnosis.** Hosty was limited to WordPress because WordPress is a
hard-coded pipeline (`sites.py` + `wordpress.py` + `php_fpm.py`), not data.
The Phase 12a Apps MVP repeated the mistake by adding a SECOND parallel
model. v2 replaces both with one abstraction.

**The model.**

- **Stack** — one deployable unit a client owns. Composed of **Services**
  (containers), **Volumes** (persistent dirs), **Endpoints** (domain →
  service:port routes through Caddy), and a reference to the **Blueprint**
  that stamped it out.
- **Blueprint** — a typed, versioned recipe living in the repo as Python
  (not YAML string-templating — type-checked, unit-tested, no injection
  surface): services + pinned images, volume layout, env contract
  (generated secrets vs user inputs), the web service, health semantics,
  backup hooks (e.g. dump-before-snapshot), and **typed day-2 Actions**
  (`wordpress.login_link`, `wordpress.rotate_salts`, …). Blueprints stay
  attached for the stack's life — they are the product moat, generalizing
  the WordPress advantage. Blueprint #1 is WordPress (full action parity
  with v1); blueprint #0 is `raw-image` (bring-your-own-container).
- **Per-stack databases.** Every stack that needs a DB carries its own
  MariaDB/Postgres service. Host MariaDB, PHP-FPM, WP-CLI, host Adminer and
  host Filebrowser all leave the provisioner; the host stack shrinks to
  Caddy + PowerDNS + the panel + the container runtime. Adminer/Filebrowser
  return as panel-owned stacks (the system hosts its own tooling).

**Orchestration: desired state + reconciler.** The DB stores the desired
spec. A **pure planner** — zero I/O, the most-tested code in the system —
computes `diff(desired, observed) → ordered Actions`; a thin executor maps
Actions onto adapters; a periodic reconciler (and panel startup, like the
existing Caddy republish) heals drift and raises notifications when
divergence persists. Create/delete become spec edits plus convergence; the
operation UI shows the planner's actions as steps. Compensating-undo
pipelines disappear — a failed create is just desired≠observed, retried by
the loop. This extends the proven `build_config()` pattern from Caddy to
the entire runtime.

**Runtime: rootless Podman, one instance per tenant, driven by systemd
(Quadlet).** Each client account maps to a dedicated Linux user with a
subuid/subgid range and lingering enabled. Their containers run rootless
under their own uid as systemd user units generated from panel-written
Quadlet files. Consequences:

- Tenant isolation is enforced by the KERNEL (user namespaces), not by
  panel code being correct. There is no root-equivalent daemon socket on
  the host at all.
- Per-tenant quotas land on the user's systemd slice — the existing slice
  machinery now caps a tenant's ENTIRE container fleet.
- Lifecycle, restarts, and logs ride systemd/journald through the existing
  systemd seam; the engine being daemonless removes the
  one-restart-kills-every-tenant failure mode.
- Networking: rootless pasta; services publish to panel-allocated loopback
  ports only; Caddy remains the single public ingress (ADR-011's one-proxy
  rule). Per-stack podman networks give service-to-service DNS inside a
  stack.
- Volumes live under the tenant's home (`…/stacks/<stack>/volumes/<name>`),
  owned by the tenant's mapped uids; root-run backups read them directly in
  the ADR-010 format via blueprint backup hooks.

**What survives untouched** (the business layer is the other moat): auth /
2FA / sessions / impersonation, users / plans / quotas, suspension-503
semantics, audit, notifications, DNS + Cloudflare, SMTP, the backup engine
(sources change, format does not), the Caddy desired-state module, the
`runner` argv discipline, and SQLite as the panel store (boring tech, the
panel must boot before any runtime exists).

**Module shape** (hexagonal, the existing discipline made explicit):
`domain/` — specs and the pure planner, importing nothing with I/O;
`adapters/` — quadlet writer, systemd-user control, podman observe, fs,
caddy, pdns: thin, argv-built, contract-tested; `orchestration/` — the
reconciler, operations, blueprint engine; `api/` — unchanged style.

**The purge (no patching).** Deleted in one release: `services/sites.py`
provisioning, `php_fpm.py`, native `wordpress.py` pipeline, `staging.py`,
`site_import.py` in their current form, the Phase 12a Apps MVP
(`system/docker.py`, `services/apps.py`, apps routes/model — salvaging its
validation grammar and test patterns), and the provisioner's PHP / MariaDB
/ WP-CLI / Adminer / Filebrowser sections. The Sites/Apps UI merges into
one Stacks UI.

Rejected alternatives: root Docker single daemon (ecosystem-maximal but
tenancy enforced in software; userns-remap is global and the socket is
root-equivalent); Kubernetes/k3s (wrong weight class, again); YAML
blueprint DSL (stringly-typed, injection-prone, untestable); greenfield
repo (re-porting the business layer buys nothing the in-repo cut doesn't).
