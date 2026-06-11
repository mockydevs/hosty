# Hosty — Hosting Panel Project Plan

A clean, modern, fast, reliable, responsive hosting control panel (CyberPanel-lite, no email).
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
- [ ] Create reproducible dev VM: Multipass or Vagrant box running Ubuntu 24.04
- [ ] Provisioning script installs: Caddy, PHP-FPM (8.3), MariaDB, PowerDNS, Filebrowser, WP-CLI
- [ ] Document one-command dev setup in README (`make dev` or `just dev`)
- [ ] Backend connects to VM over SSH or runs inside it — decide and document
- [ ] Smoke test: manually create a vhost in Caddy on the VM, serve a PHP file
- [ ] **Milestone: clone → running dev environment in under 15 minutes**

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
- [ ] Integration tests against the dev VM (pytest marker `@vm`)
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
- [ ] Install + manage PHP 8.2/8.3/8.4 via ondrej PPA in provisioning script
- [ ] Pool config template per site (unique user, socket, sane limits) — snapshot tested
- [ ] Switch PHP version per site: rewrite pool + Caddy upstream + reload, zero downtime
- [ ] Per-site PHP settings (memory_limit, upload_max_filesize) editable in UI

### Week 12: WordPress one-click install
- [ ] `services/wordpress.py` wrapping WP-CLI: download (cached), config, install, set admin
- [ ] Auto-provision MariaDB database + user per WP site (least privilege, random creds)
- [ ] Install runs as the site's Linux user (never root)
- [ ] Idempotency + failure rollback (partial install cleaned up)

### Week 13: WordPress management UI
- [ ] Install wizard: site title, admin user, locale, version
- [ ] WP site card: version, update available, plugin/theme counts (via WP-CLI)
- [ ] Actions: update core, enable/disable maintenance mode, regenerate salts, one-click admin login (magic link via WP-CLI)
- [ ] Tests: full WP install integration test on VM
- [ ] **Milestone: one click → working WordPress with HTTPS**

---

# Phase 5 — Databases (Weeks 14–15)

### Week 14: MariaDB management
- [ ] `services/mariadb.py`: create/drop database, create/drop user, grants — parameterized SQL only
- [ ] Databases linked to sites in panel DB; orphan detection
- [ ] Generated credentials shown once, stored hashed/never plaintext where possible
- [ ] Reset-password action

### Week 15: Databases UI + Adminer
- [ ] DB list per site + global list; create/delete with confirm
- [ ] Adminer deployment: served on panel-controlled path behind panel auth (signed one-time auto-login if feasible — investigate, else document tradeoff)
- [ ] Tests: SQL builders (injection attempts), full DB lifecycle on VM
- [ ] **Milestone: create DB in UI, open it in Adminer, query it**

---

# Phase 6 — File Manager (Week 16)

- [ ] Filebrowser: one instance, scoped per site directory, behind panel reverse-proxy path
- [ ] Auto-auth from panel session (Filebrowser JWT) — user lands directly in their site's files
- [ ] Embed in site detail "Files" tab (iframe) + "open full screen" link
- [ ] Verify permission boundaries: site A user cannot browse site B (test this)
- [ ] **Milestone: edit a file from the panel, see the change live**

---

# Phase 7 — DNS (Weeks 17–18)

### Week 17: PowerDNS integration
- [ ] PowerDNS auth server + REST API enabled in provisioning script
- [ ] `services/dns.py`: zones CRUD, records CRUD (A, AAAA, CNAME, MX, TXT, SRV, CAA)
- [ ] Auto-create zone with sane defaults (SOA, NS, A → server IP) when a site is created (optional toggle)
- [ ] Record validation (Pydantic models per record type)

### Week 18: DNS UI
- [ ] Zone list + record editor table (inline edit, TTL, type-specific fields)
- [ ] Common templates: "point to this server", "Google Workspace MX", "SPF/DMARC for external mail"
- [ ] Tests: record validation matrix, zone lifecycle on VM
- [ ] **Milestone: `dig @server domain` returns records managed in UI**

---

# Phase 8 — Backups (Weeks 19–21)

### Week 19: Backup engine
- [ ] `services/backup.py`: per-site backup = files tar.zst + mysqldump, manifest.json (versions, checksums)
- [ ] Run as background job with progress; concurrency limit
- [ ] Local retention policy (keep N, prune)
- [ ] Restore: full and files-only/db-only, to same site

### Week 20: S3 remote storage
- [ ] S3-compatible target config (endpoint, bucket, creds encrypted at rest in panel DB)
- [ ] Upload with multipart + retry; verify checksum after upload
- [ ] Restore-from-S3 path
- [ ] Test against MinIO in dev VM (no cloud account needed)

### Week 21: Backup scheduling + UI
- [ ] Scheduler (APScheduler or systemd timers — ADR) for per-site cron schedules
- [ ] Backups UI: list with size/date/location badges, run-now, restore wizard with explicit confirmation, schedule editor
- [ ] Failure notifications surfaced on dashboard
- [ ] Disaster drill: restore a WP site from S3 onto a fresh VM — document the runbook
- [ ] **Milestone: scheduled backup lands in MinIO and restores cleanly**

---

# Phase 9 — Hardening, Quality, Performance (Weeks 22–24)

### Week 22: Security audit
- [ ] Threat-model pass: every endpoint — authz checked? input validated? action logged?
- [ ] Audit log: who did what when (all mutating operations) + UI view
- [ ] Dependency audit: `uv` + `pnpm audit`, enable Dependabot/Renovate
- [ ] Panel served only via HTTPS on its own port/domain through Caddy; optional IP allowlist
- [ ] Run a scanner (e.g., OWASP ZAP baseline) against the panel; fix findings
- [ ] Verify again: no `shell=True`, no string-built SQL, no root-run WP-CLI (grep CI check that fails the build)

### Week 23: Reliability & test depth
- [ ] Coverage target: ≥85% on `system/` and `services/`, enforced in CI
- [ ] Chaos cases: kill MariaDB mid-provision, Caddy API down, disk full during backup — graceful errors, no corrupt state
- [ ] Panel self-recovery: systemd unit for panel with restart policy; panel DB backup of itself
- [ ] Playwright E2E suite: auth, create site, install WP, backup/restore (runs in CI against VM weekly)

### Week 24: Performance & UX polish
- [ ] API p95 < 100ms for reads (profile, add caching where measured-slow only)
- [ ] Frontend: route-level code splitting, bundle budget (< 300KB initial), Lighthouse ≥ 95 perf/accessibility
- [ ] Accessibility pass: keyboard nav, focus traps in dialogs, aria labels
- [ ] Mobile pass on real phone: every flow usable
- [ ] Empty states, skeleton loaders, optimistic updates where safe

---

# Phase 10 — Installer, Docs & v1.0 Release (Weeks 25–26)

### Week 25: Production installer
- [ ] `install.sh`: idempotent installer for fresh Ubuntu 24.04 — installs stack, creates panel user, systemd units, prints initial login
- [ ] Panel self-update mechanism (git tag based or release tarball) — keep simple
- [ ] Uninstall script
- [ ] Test installer on a clean cloud VM (e.g., Hetzner/DO smallest instance) — twice (idempotency)

### Week 26: Documentation & release
- [ ] README: screenshots, features, install one-liner, requirements
- [ ] `docs/`: admin guide, backup/restore runbook, architecture overview, API reference (auto from OpenAPI)
- [ ] CHANGELOG.md, tag `v1.0.0`, GitHub release
- [ ] Post-1.0 backlog file: multi-user/roles, 2FA, monitoring graphs, staging clones, Redis cache to