# Hosty — Hosting Panel Project Plan

A clean, modern, fast, reliable, responsive hosting control panel — **HostyPanel** (no email hosting).
Built from scratch. Hobby pace: **~10 hrs/week → ~26 weeks**.

---

## Tech Stack (pinned at project start)

| Layer | Choice | Notes |
|---|---|---|
| OS target | Ubuntu 24.04 LTS | Latest LTS — not 22.04 |
| Backend | FastAPI + Python 3.12+ | Async, Pydantic v2 |
| Package mgmt (py) | `uv` | Replaces pip/poetry — fast, lockfile |
| Lint/format (py) | Ruff | Lint + format in one tool |
| ORM / DB | SQLAlchemy 2.0 + SQLite (WAL mode) | Alembic for migrations |
| Frontend | React 19 + TypeScript + Vite | |
| Styling | Tailwind CSS v4 + shadcn/ui | |
| Data fetching | TanStack Query | Cache, retry, optimistic updates |
| Routing | TanStack Router or React Router 7 | Type-safe routes |
| Validation | Zod (FE) / Pydantic (BE) | Schema-first both ends |
| Package mgmt (js) | pnpm | |
| Lint/format (js) | Biome (or ESLint + Prettier) | |
| Web server | Caddy 2 | Admin API for config, auto-HTTPS |
| PHP | PHP-FPM 8.2 / 8.3 / 8.4 pools | Via `ondrej/php` PPA |
| WordPress | WP-CLI | |
| Site DBs | MariaDB | |
| DB admin UI | Adminer | |
| File manager | Filebrowser | |
| DNS | PowerDNS + its REST API | |
| Backups | tar + mysqldump → local + S3 (boto3) | |
| Process mgmt | systemd (via `subprocess`) | |
| Auth | JWT (short-lived) + refresh, Argon2id hashing | |
| CI | GitHub Actions | Lint, type-check, test on every push |
| Tests | pytest + httpx (BE), Vitest + Testing Library (FE) | |

## Engineering Principles (apply to every task)

- **No technical debt by default**: every PR-sized chunk leaves the repo releasable. No "fix later" comments without a tracked task.
- **Type safety end-to-end**: mypy/pyright strict on backend, TS strict mode on frontend, OpenAPI-generated client types.
- **All system mutations go through one layer**: a single `system/` module owns every shell call. No `subprocess` anywhere else. Every command logged, validated, and tested.
- **Never trust input**: validate at API boundary (Pydantic), sanitize anything that reaches a shell (use arg lists, never `shell=True` with user input).
- **Idempotent operations**: creating a vhost that exists, deleting one that doesn't — handled gracefully.
- **Small commits, conventional commits format** (`feat:`, `fix:`, `chore:`...).
- **Test the risky parts first**: shell-command builders and Caddy config generation get unit tests before UI polish does.

---

# Phase 0 — Foundation & Dev Environment (Weeks 1–2)

### Week 1: Repo, tooling, conventions
- [x] Create repo structure: monorepo `backend/`, `frontend/`, `installer/`, `docs/`
- [x] Initialize git, `.gitignore`, `.editorconfig`, MIT license, README skeleton
- [ ] Backend: init with `uv`, FastAPI hello-world, Ruff + pyright strict configured
- [ ] Frontend: Vite + React 19 + TS strict + Tailwind v4 + shadcn/ui initialized
- [x] Add pre-commit hooks (Ruff, Biome, type-check) via `pre-commit` or lefthook
- [x] GitHub Actions CI: lint + type-check + test jobs for both apps, must pass to merge
- [x] Write `docs/ARCHITECTURE.md`: decisions log (ADR-style), starting with Caddy-via-Admin-API vs file-based config — **decide now**
- [x] Write `docs/CONVENTIONS.md`: commit format, branch strategy, code style rules

### Week 2: Dev environment that mirrors production
- [x] Create reproducible dev VM: Multipass Ubuntu 24.04, scripted in `installer/dev-vm/` (`dev-vm.ps1` / `dev-vm.sh`) — verified on Windows/Hyper-V
- [x] Provisioning script installs: Caddy, PHP-FPM (8.2–8.4), MariaDB, PowerDNS (gsqlite3 + REST API), Filebrowser, WP-CLI (`installer/provision.sh` — ran clean on the VM after two real-world fixes: resolved stub listener vs port 53, Adminer v5 asset rename)
- [x] Document one-command dev setup in README (`dev-vm up` instead of make/just — works on Windows hosts too)
- [x] Backend connects to VM over SSH or runs inside it — decided: runs inside, as root, repo mounted; ADR-009
- [x] Smoke test: create a vhost in Caddy on the VM, serve a PHP file — `smoke.sh` PASSED (php8.3.31 via admin-API vhost)
- [x] **Milestone: clone → running dev environment in under 15 minutes** — `dev-vm up` does launch → provision → deps → schema in one command

---

# Phase 1 — Backend Core (Weeks 3–5)

### Week 3: Application skeleton
- [x] FastAPI project layout: `api/` (routers), `core/` (config, security), `db/` (models, session), `system/` (shell layer), `services/` (business logic)
- [x] Settings via Pydantic Settings (env vars, `.env` for dev)
- [x] SQLite with WAL mode + SQLAlchemy 2.0 async + Alembic baseline migration
- [x] Structured logging (structlog): JSON in prod, pretty in dev, request IDs
- [x] Global exception handlers → consistent JSON error envelope
- [x] Health endpoint `/api/health` reporting service statuses

### Week 4: Auth & security baseline
- [x] User model (single admin user for v1, but schema supports roles)
- [x] Argon2id password hashing
- [x] JWT access tokens (15 min) + refresh tokens (httpOnly secure cookie, rotation)
- [x] Login, logout, refresh, change-password endpoints
- [x] Rate limiting on auth endpoints (slowapi or custom middleware)
- [x] First-boot flow: panel generates admin password / setup token
- [x] Security headers middleware (CSP, HSTS, X-Frame-Options)
- [x] Tests: full auth flow, token expiry, rate limit, wrong password

### Week 5: The system layer (most important code in the project)
- [x] `system/runner.py`: single entrypoint for shell commands — arg-list only, timeout, captured output, structured logging, typed result object
- [x] `system/systemd.py`: start/stop/restart/status/enable for units
- [x] `system/users.py`: create/delete Linux site users (one user per site — isolation)
- [x] Permission model decision: panel runs as root vs sudo-whitelisted user — document in ADR
- [x] Unit tests for every command builder (assert exact argv, no shell injection possible)
- [x] Integration tests against the dev VM (pytest marker `@vm`) — runner working (`dev-vm test`), suite is currently 1 test and grows with each phase
- [ ] **Milestone: authenticated API that can query systemd service status on the VM**

---

# Phase 2 — Frontend Core (Weeks 6–7)

### Week 6: Shell, auth, API client
- [x] App layout: responsive sidebar (collapses to bottom-nav/drawer on mobile), header, content area
- [x] Dark/light mode (system-aware, persisted)
- [x] OpenAPI → generated TS client (openapi-ts) wired into TanStack Query
- [x] Login page, auth context, token refresh interceptor, protected routes (incl. first-boot setup form)
- [x] Error/loading/empty-state patterns: one reusable component each — used everywhere
- [x] Toast notifications (sonner)

### Week 7: Dashboard v0 + design system discipline
- [x] Dashboard page: service status cards (Caddy, MariaDB, PHP-FPM, PowerDNS), CPU/RAM/disk gauges
- [x] Backend endpoints for system stats (psutil)
- [x] Define the 6–8 shadcn components used everywhere (Table, Dialog, Form, Card, Badge, Tabs) — no one-off styles
- [x] Form pattern: react-hook-form + Zod resolver, server errors mapped to fields
- [ ] Responsive pass: test at 360px, 768px, 1280px — built responsive (mobile drawer, breakpoint grids); needs manual verification in a browser
- [x] Vitest setup + tests for auth flow and one form (8 tests passing)
- [ ] **Milestone: log in on a phone and see live service status** — needs the dev VM / running backend to verify

---

# Phase 3 — Domains & Vhosts (Weeks 8–10)

### Week 8: Caddy integration layer
- [x] `services/caddy.py`: manage config via Caddy Admin API (JSON config) — full desired-state sync, not patches
- [x] Vhost model: domain, site user, doc root, PHP version, status (`Site` + migration 0002)
- [x] Config generation pure + unit tested: model in → exact Caddy JSON out (snapshot tests)
- [x] Rollback: validate config before apply; on failure, previous config restored automatically (`CaddyClient.apply`, tested)

### Week 9: Site provisioning pipeline
- [x] Create-site flow as a transactional pipeline with compensating rollback: Linux user → doc root + skeleton → PHP-FPM pool → Caddy vhost → DB record (any step fails ⇒ undo previous steps)
- [x] Delete-site flow (with "type the domain to confirm" semantics; steps idempotent so a failed delete is retryable)
- [x] SSL: surface Caddy cert status per domain; handle DNS-not-pointing failure case with clear error (`services/ssl.py` probe)
- [x] SSL: retry/renew certificate issuance from the panel (`POST /api/sites/{id}/ssl/renew` re-applies Caddy config; button on the HTTPS certificate card)
- [x] Background task handling for slow operations (FastAPI BackgroundTasks) + operation status endpoint (`/api/operations/{id}` with per-step progress)

### Week 10: Sites UI
- [x] Sites list: searchable table, status badges (SSL, PHP version, running)
- [x] Create-site wizard (domain validation incl. punycode, PHP version pick)
- [x] Site detail page: tabs for Overview / Files / Databases / Backups (tabs stubbed for later phases)
- [x] Live operation progress (poll) during provisioning, with per-step rollback states
- [ ] E2E happy path test: create site via UI → curl the domain on the VM → 200 — needs the dev VM (Week 2 item)
- [ ] **Milestone: create a domain in the UI, get a live HTTPS site on the VM** — needs the dev VM

> Phase 3 note: minimal per-site PHP-FPM pool management (`services/php_fpm.py`,
> snapshot-tested pool template) landed early because the Week 9 pipeline needs it;
> Week 11 extends it (version switching, per-site settings UI).

---

# Phase 4 — PHP & WordPress (Weeks 11–13)

### Week 11: PHP-FPM pool management
- [x] Install + manage PHP 8.2/8.3/8.4 via ondrej PPA in provisioning script (`installer/provision.sh`, idempotent — not yet run on a VM)
- [x] Pool config template per site (unique user, socket, sane limits) — snapshot tested
- [x] Switch PHP version per site: rewrite pool + Caddy upstream + reload, zero downtime (new pool up → Caddy repointed → old pool removed; failure reverts)
- [x] Per-site PHP settings (memory_limit, upload_max_filesize) editable in UI

### Week 12: WordPress one-click install
- [x] `services/wordpress.py` wrapping WP-CLI: download (cached via shared `WP_CLI_CACHE_DIR`), config, install, set admin
- [x] Auto-provision MariaDB database + user per WP site (least privilege, random creds; minimal `services/mariadb.py` — Phase 5 extends it and should revisit CLI → driver, see module docstring)
- [x] Install runs as the site's Linux user via `runuser` (never root; argv builders unit-tested)
- [x] Idempotency + failure rollback (DB dropped, doc root restored to skeleton; `core is-installed` guards re-install)

### Week 13: WordPress management UI
- [x] Install wizard: site title, admin user/password/email, locale, version
- [x] WP site card: version, update available, plugin/theme counts (via WP-CLI)
- [x] Actions: update core, enable/disable maintenance mode, regenerate salts, one-click admin login (magic link via wp-cli login-command package, installed on first use)
- [ ] Tests: full WP install integration test on VM — needs the dev VM (Week 2 item)
- [ ] **Milestone: one click → working WordPress with HTTPS** — needs the dev VM

---

# Phase 5 — Databases (Weeks 14–15)

### Week 14: MariaDB management
- [x] `services/mariadb.py`: create/drop database, create/drop user, grants — identifiers regex-locked + panel-generated secrets only (CLI; parameterization equivalence + revisit condition documented in ADR-007)
- [x] Databases linked to sites in panel DB (`databases` table, migration 0004; WP installs register theirs, site delete drops them all); orphan + missing-on-server detection
- [x] Generated credentials shown once, stored as SHA-256 hash only
- [x] Reset-password action

### Week 15: Databases UI + Adminer
- [x] DB list per site + global list; create/delete with type-name confirm; show-once credentials dialog
- [x] Adminer deployment: internal-only Caddy listener + dedicated locked-down PHP pool, reached via the panel's authenticated `/adminer` proxy (60s HMAC ticket → scoped session cookie); credential auto-fill deferred — tradeoff documented in ADR-007
- [x] Tests: SQL builders (injection attempts), ticket auth, proxy via mock upstream — full DB lifecycle on VM still pending (needs the dev VM)
- [ ] **Milestone: create DB in UI, open it in Adminer, query it** — needs the dev VM

---

# Phase 6 — File Manager (Week 16)

- [x] Filebrowser: one instance, scoped per site directory, behind panel reverse-proxy path (internal-only listener, provisioned + systemd unit in `installer/provision.sh` — not yet run on a VM)
- [x] Auto-auth from panel session — proxy auth (`X-Hosty-Fb-User` header from signed HMAC ticket → scoped session cookie) instead of Filebrowser JWT; tradeoff documented in ADR-008
- [x] Embed in site detail "Files" tab (iframe) + "open full screen" link
- [x] Verify permission boundaries: per-site Filebrowser user scoped to its own directory; tests cover signature-bound scope, spoofed-header stripping, anonymous rejection — live two-site boundary check on VM still pending (needs the dev VM)
- [ ] **Milestone: edit a file from the panel, see the change live** — needs the dev VM
- [ ] Ownership normalization: Filebrowser runs as root, created files are root-owned until event-hook `chown` is configured (see ADR-008) — verify on VM

---

# Phase 7 — DNS (Weeks 17–18)

### Week 17: PowerDNS integration
- [x] PowerDNS auth server + REST API enabled in provisioning script
- [x] `services/dns.py`: zones CRUD, records CRUD (A, AAAA, CNAME, MX, TXT, SRV, CAA)
- [x] Auto-create zone with sane defaults (SOA, NS, A → server IP) when a site is created (optional toggle) — wizard toggle for every account; plus sites↔DNS bridge: DNS page lists hosted sites without a zone (one-click create, zone ownership follows the site owner via `site_id` on `POST /api/dns/zones`), and the site Overview gets a DNS card linking to / creating its zone; installer now auto-detects `HOSTY_PUBLIC_IP`
- [x] Record validation (Pydantic models per record type)

### Week 18: DNS UI
- [x] Zone list + record editor table (inline edit, TTL, type-specific fields)
- [x] Common templates: "point to this server", "Google Workspace MX", "SPF/DMARC for external mail"
- [x] Cloudflare integration: `services/cloudflare.py` + one-click **Push to Cloudflare** per zone — creates/updates/skips records, never deletes; needs `HOSTY_CLOUDFLARE_API_TOKEN` (Zone.DNS edit) and the domain already added in Cloudflare
- [x] One-click **Pull from Cloudflare** per zone (`POST /api/dns/zones/{id}/pull/cloudflare`): imports the domain's current Cloudflare records into the panel zone — create/update only, panel-only records never deleted, SOA/apex-NS untouched; same per-user token resolution as push
- [x] Delegation detection (`GET /api/dns/zones/{id}/delegation`): live NS lookup via DNS-over-HTTPS + NS→A resolution compared to `HOSTY_PUBLIC_IP`; zone page banner says whether records here are live or the registrar delegates elsewhere (e.g. Cloudflare) — advisory only, lookup failures never break the page
- [ ] Verify Cloudflare push against a real Cloudflare account/token
- [x] Cloudflare token managed in the UI: Settings card verifies the token (`/user/tokens/verify`) and stores it encrypted in `panel_settings` (`services/cloudflare_config.py`); env var remains a bootstrap fallback
- [x] Cloudflare account management: list zones + add/edit/delete DNS records (A/AAAA/CNAME/TXT/MX/NS, proxied toggle) under `/api/dns/cloudflare/*` with `/dns/cloudflare` UI
- [ ] Verify Cloudflare zone/record management against a real Cloudflare account
- [x] Panel domain & HTTPS from the UI: Settings card → `PUT /api/system/panel-domain` validates DNS points here (overridable), publishes the panel vhost to Caddy (auto-cert), flips `HOSTY_COOKIE_SECURE=true`, and persists both to the env file (`services/panel_config.py`); removal reverts everything
- [x] Cloudflare proxy support: per-site "Behind Cloudflare" toggle (`PATCH /api/sites/{id}/cloudflare-proxy`, migration 0008) — Caddy issues an internal origin certificate for proxied domains instead of attempting ACME HTTP-01; SSL probe reports `origin_internal`; hint shown when proxying a record in the Cloudflare UI (requires Cloudflare SSL mode "Full")
- [x] Tests: record validation matrix + zone lifecycle (mocked PowerDNS); VM run pending
- [ ] **Milestone: `dig @server domain` returns records managed in UI**

---

# Phase 8 — Backups (Weeks 19–21)

### Week 19: Backup engine
- [x] `services/backup.py`: per-site backup = files tar.zst + mysqldump, manifest.json (versions, checksums)
- [x] Run as background job with progress; concurrency limit (global lock, operation steps polled by the UI)
- [x] Local retention policy (keep N, prune)
- [x] Restore: full and files-only/db-only, to same site (checksum-verified first)

### Week 20: S3 remote storage
- [x] S3-compatible target config: UI-managed credentials, secret encrypted at rest in the panel DB, verified before save; per-site choice of what to back up (files / databases / S3 mirror)
- [x] Upload with multipart + retry (boto3); size-verified after upload
- [x] Restore-from-S3 path (auto-fetch when the backup is not local)
- [ ] Test against MinIO in dev VM (no cloud account needed)

### Week 21: Backup scheduling + UI
- [x] Scheduler: in-process asyncio tick (ADR-010), per-site daily/weekly at HH:00
- [x] Backups UI: list with size/date/location badges, run-now, restore wizard with explicit confirmation, schedule editor
- [ ] Failure notifications surfaced on dashboard
- [ ] Disaster drill: restore a WP site from S3 onto a fresh VM — document the runbook
- [ ] **Milestone: scheduled backup lands in MinIO and restores cleanly**

---

# Phase 9 — Hardening, Quality, Performance (Weeks 22–24)

### Week 22: Security audit
- [x] Threat-model pass: every endpoint — authz checked? input validated? action logged? (`docs/SECURITY.md`)
- [x] Audit log: who did what when (all mutating operations, bodies never stored) + Settings UI view
- [x] Dependency audit: Dependabot enabled (uv + npm + actions, weekly)
- [x] Panel served via HTTPS through Caddy (`HOSTY_PANEL_DOMAIN` vhost, published at startup) + IP allowlist at both Caddy and app layer
- [ ] Run a scanner (e.g., OWASP ZAP baseline) against the panel; fix findings — VM round
- [x] Verify again: no `shell=True`, no string-built SQL outside the validated module, no root-run WP-CLI (`scripts/forbidden_patterns.sh`, fails the build)

### Week 23: Reliability & test depth
- [x] Coverage target: ≥85% on `system/` and `services/`, enforced in CI (`--cov-fail-under=85`)
- [x] Chaos cases: MariaDB dies mid-WP-install and at provisioning, Caddy API down on create AND delete (delete retryable), disk full mid-backup — graceful errors, rollbacks verified, no corrupt state (`tests/test_chaos.py`)
- [x] Panel self-recovery: `installer/systemd/hosty.service` (Restart=always + start limits); daily panel DB self-backup via `VACUUM INTO`, 7 kept
- [x] Playwright E2E suite (auth, create site, install WP, backup) + weekly CI workflow — first execution pending the VM round (needs a runner that reaches the VM)

### Week 24: Performance & UX polish
- [ ] API p95 < 100ms for reads (profile, add caching where measured-slow only) — measure on the VM
- [x] Frontend: route-level code splitting (every page lazy); initial payload 114KB gzipped, <300KB budget enforced in CI (`scripts/check_bundle_budget.mjs`); Lighthouse ≥95 — VM round
- [x] Accessibility pass: all icon buttons labeled, native-dialog focus traps, ARIA tabs with arrow-key nav, labeled forms with `role="alert"` errors
- [ ] Mobile pass on real phone: every flow usable — VM round
- [x] Empty states, skeleton loaders throughout; optimistic updates deliberately omitted (system mutations are pipelines with progress UI)

---

# Phase 10 — Installer, Docs & v1.0 Release (Weeks 25–26)

### Week 25: Production installer
- [x] `install.sh`: idempotent installer for fresh Ubuntu 24.04 — stack, frontend build, migrations, systemd unit, prints panel URL + first-boot instructions
- [x] Panel self-update mechanism (`update.sh`, git ref/tag based)
- [x] Uninstall script (keeps user data by default; `--purge-all` for everything)
- [ ] Test installer on a clean cloud VM (e.g., Hetzner/DO smallest instance) — twice (idempotency) — VM round

### Week 26: Documentation & release
- [x] README: features, install one-liner, requirements (screenshots placeholder — captured during the VM round)
- [x] `docs/`: admin guide, backup/restore runbook, architecture overview, security model; API reference served live at `/api/docs` (static export in backlog)
- [x] CHANGELOG.md (`v1.0.0-rc.1`); final `v1.0.0` tag + GitHub release after the VM verification round
- [x] Post-1.0 backlog file (`docs/BACKLOG.md`)
- [ ] **Milestone: a stranger can install and host a WordPress site using only the README** — proven by the installer test in the VM round

---

# Phase 11 — Multi-tenancy & User Management (post-1.0)

Goal: a developer/admin installs Hosty once and hosts multiple client users.
Each client sees and manages ONLY their own services; the admin sees everything.

### Phase 11a: Ownership & scoping (core)
- [x] Migration: `sites.owner_id` (FK users, existing sites → admin); `users.must_change_password`, `users.suspended`, `users.max_sites`, `users.max_databases`
- [x] AuthZ layer: `require_owner_or_admin` dependency; every sites/databases/backups/operations route scoped to owner (admin unrestricted); 404 (not 403) for other tenants' resources to avoid existence leaks
- [x] Users API (admin-only): create client with temp password (forced change on first login), suspend/unsuspend, delete (choose: delete sites too, or reassign to admin), set quotas
- [x] Quota enforcement on site/database create (max_sites, max_databases per client)
- [x] Audit log scoping: clients see only their own entries; admin sees all
- [x] Users page in sidebar (admin-only): list, create, suspend, quotas, delete
- [x] Client UX: dashboard/site list/databases/backups filtered to own resources; Settings shows only change-password for clients (panel domain + Cloudflare token remain admin-only)

### Phase 11b: DNS & Cloudflare for clients
- [x] Zone ownership table (PowerDNS zones are external — map zone name → owner); clients create and manage their own zones + records, admin sees all
- [x] Per-client Cloudflare tokens: stored encrypted per user (`panel_settings` key `cloudflare:{user_id}` or a `user_settings` table); the Settings Cloudflare card works for every user against their own account
- [x] Cloudflare zones/records/push endpoints resolve the CURRENT USER's token — each client sees only their own Cloudflare account's zones (natural isolation); env-var token remains an admin-only fallback
- [x] Push-to-Cloudflare for a PowerDNS zone uses the zone owner's token; ADMIN OVERRIDE: the admin can always use their own Cloudflare token to set up or push DNS for ANY zone on the server (e.g. onboarding a client whose domain sits in the admin's CF account)

### Phase 11c: Resource scoping
- [x] systemd slices per site user: CPUQuota + MemoryMax set from per-client limits
- [x] Disk quotas per site user (filesystem quota or du-based soft limits with warnings)
- [x] Per-client usage view (their sites' CPU/mem/disk), admin keeps whole-server stats

### Phase 11d: Hosting-business features (expert backlog)
- [x] Impersonation: admin "log in as client" for support — loudly audited, visible banner in the UI while impersonating (`POST /api/users/{id}/impersonate`; `ImpersonationBanner` in app-layout; auth context `impersonate`/`stopImpersonating`)
- [x] Suspension semantics: suspending a client takes their sites offline with a 503 "account suspended" Caddy page (not just a login block) — republishes the owner's vhosts on suspend/unsuspend
- [x] Admin notifications: disk nearly full, managed service down, backup failed, repeated cert-issuance failures (dashboard `NotificationsCard` + best-effort webhook via Settings `NotificationWebhookCard`)
- [x] Usage metering per client: disk (du per site user), DB size, bandwidth from Caddy access logs per vhost; exportable monthly summary (Usage page: own usage + admin per-client table + CSV export)
- [x] Plans: named quota bundles (e.g. Starter 1 site/1 DB, Pro 5/10) assignable to clients instead of raw numbers (`PlansSection` in Users page; `plan_id` on quota editor)
- [x] 2FA (TOTP) for all accounts; active-sessions view with revoke; admin view of failed-login attempts (login TOTP step + `TwoFactorCard`/`SessionsCard`; admin reset-2FA on users page)
- [x] Site migration/import: rsync files in, import SQL dump, wp-cli search-replace for the domain (site detail → Tools → `ImportCard`)
- [x] Staging clones: copy site + DB to staging.<domain>, push back to production (site detail → Tools → `StagingCard`)
- [x] Per-site PHP error log viewer for clients (site detail → Tools → `PhpLogCard`)

> Phase 11 status: backend committed (`feat(backend): multi-tenancy phase 11b/c/d`,
> 517 tests green); frontend wired and verified locally — `tsc --noEmit`, `vite build`,
> `biome check`, and the 48-test Vitest suite all pass. Live VM verification of the new
> flows still pending (same VM round the rest of the project is waiting on).

---

# v2 — Container-native rearchitecture (ADR-013)

**Detailed working plan: [docs/V2-PLAN.md](docs/V2-PLAN.md)** — exact
modules, schemas, semantics, test plans, and per-milestone gates. Read it
before working any milestone; milestones are strictly sequential.

Quality-first rebuild, no legacy obligation: ONE workload model (Stacks +
Blueprints), a pure-planner reconciler, rootless Podman per tenant via
systemd/Quadlet. The business layer (auth, tenancy, quotas, suspension,
DNS, backups, audit) survives untouched; the workload half is replaced.
The Phase 12a Apps MVP is superseded and gets deleted in M6 (its validation
grammar and test patterns are salvaged).

### M0 — Host foundation (VM-verified before anything else)
- [x] Provisioner: podman + uidmap + passt + crun (Ubuntu 24.04 packages), no PHP/MariaDB/WP-CLI on fresh installs yet (they leave in M6)
- [x] Tenant user manager: client account → dedicated Linux user, subuid/subgid range, lingering enabled, home layout `~/stacks/<stack>/volumes/<name>` (`system/tenants.py` + `services/tenancy.py` ledger + migration 0014)
- [ ] Spike (throwaway, VM): panel-written Quadlet file → `systemctl --machine <user>@.host --user` start → rootless container publishes 127.0.0.1:<port> → Caddy proxies it; journald logs readable; slice caps apply. This validates EVERY risky mechanism before the core is built — `installer/dev-vm/spike-quadlet.sh` is written; BLOCKED on dev-VM access (local Multipass daemon unresponsive), run it before M2's VM round-trip
- [ ] **Milestone: a hand-written nginx Quadlet serves through Caddy on the dev VM, rootless, slice-capped**

### M1 — Domain core (pure, zero I/O)
- [x] Specs: StackSpec / ServiceSpec / VolumeSpec / EndpointSpec — ORM-free, frozen dataclasses
- [x] Pure planner: `plan(desired, observed) → [Action]` covering create, delete, image change, env change, scale-to-zero (suspension), drift repair; exhaustive unit tests INCLUDING property-based convergence (`apply(plan) ⇒ observed == desired`)
- [x] Validation grammar (salvaged from 12a): image refs, env keys, mount paths, names — option-injection rejected by construction
- [x] **Milestone: planner handles every lifecycle as data, 100% branch-covered, no adapter exists yet** (CI enforces `--cov=app.domain --cov-branch --cov-fail-under=100`)

### M2 — Adapters (thin, contract-tested)
- [x] Quadlet writer: ServiceSpec → `.container`/`.network`/`.volume` unit text (pure builders, snapshot-tested) + root-managed placement per tenant (`system/quadlet.py`)
- [x] systemd-user control + observe: start/stop/daemon-reload/show via `--machine <user>@.host --user`; observed state primarily from systemd, container detail via per-user podman socket (`system/systemd_user.py`, `system/podman.py`)
- [x] Loopback port allocator (DB-ledger, UNIQUE-constraint race-safe — pattern from 12a; `services/ports.py`, apps allocator delegates)
- [x] Caddy: EndpointSpec joins `build_config` (`StackRoute` + `routes_for_stack`; Site/App specs remain until M6)
- [ ] **Milestone: adapters round-trip a StackSpec on the VM end-to-end, driven only by tests** — `tests/test_vm_stack_roundtrip.py` written; run `dev-vm.ps1 test` once the dev VM is back (with the M0 spike)

### M3 — Orchestration
- [x] Reconciler loop: on-startup + interval converge, per-stack serialization, planner actions recorded as operation steps (existing operations UI contract) — `orchestration/{reconciler,executor,observer,operations}.py` + `system/stackhost.py`; wired into the app lifespan (`reconcile_*` settings)
- [x] Drift notifications (reuse dedupe_key) when divergence persists across N cycles (`stack:<name>:drift`, default 3, resolves on convergence)
- [x] Suspension = desired state scale-to-zero + Caddy 503 (one mechanism, no special case)
- [ ] **Milestone: kill a container by hand on the VM; the panel converges and notifies within one cycle** — FakeHost suite green; `tests/test_vm_reconcile.py` written, run with the other VM gates once the dev VM is back

### M4 — Blueprint engine + Stacks API/UI
- [x] Blueprint contract: typed Python registry — services, volumes, env contract (generated secrets / user inputs), web service, health, backup hooks, day-2 Actions (`orchestration/blueprints/base.py`: pydantic `inputs()` drives the UI form; `render` runs at create/upgrade only)
- [x] Blueprint #0 `raw-image`: image + port + env + volumes (covers Django/Next.js/anything)
- [x] Stacks API: CRUD + actions + logs, owner-scoped (404-not-403), `max_stacks` quota in plans/users/quotas + admin routes (`api/routes/stacks.py`; reconciler fully wired: DB-backed desired state, ingress sync, status projection; deletion = convergence toward absence)
- [x] Stacks UI: list with convergence-driven status pills, create wizard rendered from the blueprint inputs JSON Schema (zero per-blueprint UI code), detail page (services, endpoints, journald logs, actions with confirm + show-once, type-to-confirm delete)
- [ ] **Milestone: a client deploys a Next.js image with custom domain + HTTPS from the UI** — FakeHost API suite + Vitest green; run the e2e on the dev VM with the other queued VM gates (M0 spike, M2 round-trip, M3 reconcile)

### M5 — WordPress blueprint (the moat, at full parity)
- [x] Composition: `wordpress:<php>-apache` (pinned digest) + per-stack MariaDB service + wp-content volume
- [x] Actions at v1 parity: install, core/plugin/theme updates, maintenance mode, salt rotation, one-time admin login link — via Podman-backed WP-CLI behind the adapter seam
- [x] Backup hooks: mysqldump-before-snapshot + stack volume archive → ADR-010 format unchanged; owner-scoped backup/restore API + stack-detail UI, checksum verification, S3 fetch/mirror, stop/restore/start ordering covered by FakeHost tests
- [x] Per-stack Adminer/Filebrowser access re-pointed at stack volumes/DBs via the existing ticket proxy
- [ ] **Milestone: one-click WordPress, fully containerized, with every v1 convenience**

### M6 — The purge
- [ ] Delete: native sites provisioning, php_fpm, wordpress pipeline, staging, site_import (rebuilt later as blueprint features), Apps MVP (docker.py, apps service/routes/model), Sites/Apps UI
- [ ] Provisioner drops PHP / host MariaDB / WP-CLI / host Adminer / host Filebrowser
- [ ] Migrations: drop dead tables; docs + CHANGELOG; major version bump
- [ ] **Milestone: `grep -r php_fpm backend/` returns nothing; fresh install hosts WordPress + Django side by side, all containers**

---
## Recurring (every week)

- [ ] All CI checks green before merge — never bypass
- [ ] Update ADRs when any decision changes
- [ ] 30 min: dependency updates review
- [ ] Keep `TASKS.md` honest — check items off, add discovered tasks immediately

## Definition of Done (every task)

1. Typed, linted, formatted — CI green
2. Tests for new logic (unit minimum; integration for system-touching code)
3. Errors handled and surfaced clearly in UI
4. Works on mobile viewport if it has UI
5. No TODO without a corresponding tracked task
