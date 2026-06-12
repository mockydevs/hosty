# Changelog

## Unreleased

### Added
- **v2/M0 — tenant foundation (ADR-013)**: per-client Linux users
  (`hosty-t-<id>`) with panel-allocated subuid/subgid ranges (ledger in the
  new `tenants` table, migration 0014), lingering user managers, and a
  hardened argv seam (`system/tenants.py`). Provisioner installs the
  rootless Podman runtime (podman, uidmap, passt, crun, systemd-container).
  The Quadlet spike script (`installer/dev-vm/spike-quadlet.sh`) documents
  and verifies every risky host mechanism on the dev VM.
- **v2/M1 — pure domain core (ADR-013)**: frozen ORM-free specs
  (`domain/specs.py`), a single-source validation grammar rejecting argv/
  unit-file injection by construction (`domain/validate.py`), the Action
  union (`domain/actions.py`), and the pure reconciliation planner
  (`domain/planner.py`) covering create, delete, spec change, suspension
  (scale-to-zero), resume, drift repair, and tenant migration. Property
  tests (hypothesis) prove convergence and idempotency over a model host;
  CI enforces 100% branch coverage on `app/domain`.
- **v2/M2 — host adapters (ADR-013)**: pure Quadlet renderers with
  always-on hardening (NoNewPrivileges, loopback-only PublishPort,
  journald logging, spec-hash markers, env via 0600 EnvironmentFile —
  `system/quadlet.py`), tenant user-manager control via
  `systemctl --machine <user>@.host --user` with the same graceful-
  degradation contract as host systemd (`system/systemd_user.py`),
  rootless Podman observe/exec through per-tenant sockets with total
  parsers (`system/podman.py`), a shared loopback port-ledger allocator
  (`services/ports.py` — the 12a apps allocator now delegates), and stack
  endpoint routes joining the Caddy config builder (suspension 503,
  Cloudflare internal TLS). VM round-trip test added (`-m vm`).
- **v2/M3 — orchestration (ADR-013)**: the reconciler is now the only
  writer to the host — converge on startup, on an interval
  (`reconcile_interval_seconds`), and on demand after API writes; per-stack
  locks with a global concurrency cap; planner actions recorded as
  operation steps (UI contract unchanged); undo-free failure semantics
  (a failed action degrades the stack and the next cycle replans);
  persistent-drift notifications (`stack:<name>:drift` after N cycles,
  auto-resolved on convergence). Unit-file ownership is attributed by
  content markers, making hyphenated stack/service names unambiguous.
- **Email delivery**: outgoing SMTP account in Settings, used for temporary
  passwords, panel alert emails (configurable recipients) and a send-test
  button. Credentials are encrypted at rest; saving verifies the connection
  and login and reports the exact failure inline.
- **Email network mode**: SMTP connects over IPv4 by default; an optional
  "IPv4 + IPv6" mode also tries IPv6 — IPv4 is always attempted first, so a
  half-configured IPv6 stack can never block outgoing mail. Connection
  failures now report every attempted address ("IPv6 unreachable; IPv4 timed
  out") instead of only the last one.
- **Panel domain, one-click DNS**: when enabling HTTPS fails because the
  domain does not resolve, a "Create the A record on the DNS page" button
  publishes `<domain> -> this server` into the most specific zone hosted on
  the panel's own DNS page, then retries automatically
  (`POST /api/system/panel-domain/dns-record`, admin-only).

### Changed
- Startup Caddy resync is now controlled by an explicit
  `HOSTY_CADDY_SYNC_ON_STARTUP` setting (default on) instead of being
  silently skipped in test environments.

### Fixed
- SMTP connection errors surface per-address details to the admin instead of
  a bare "Network is unreachable".

## v1.0.0-rc.1 — 2026-06-12

First feature-complete release candidate. Final tag `v1.0.0` follows the
on-VM verification round (E2E suite, installer on a clean cloud VM, restore
drill, ZAP baseline scan, Lighthouse/mobile pass).

### Added
- **Sites**: provisioning pipeline with compensating rollback (Linux user,
  web root, PHP-FPM pool, Caddy vhost), automatic HTTPS, live step progress,
  type-to-confirm deletion, per-domain certificate status.
- **PHP**: per-site pools for 8.2/8.3/8.4, zero-downtime version switching,
  editable memory/upload limits.
- **WordPress**: one-click install (dedicated DB, cached core download,
  runs as the site user), updates, maintenance mode, salt rotation, one-time
  admin login links.
- **Databases**: MariaDB lifecycle with show-once credentials (hashes only at
  rest), reset-password, orphan detection, Adminer behind an authenticated
  panel proxy.
- **Files**: per-site Filebrowser behind the panel proxy with signed,
  site-scoped sessions.
- **DNS**: PowerDNS zones/records (A, AAAA, CNAME, MX, TXT, SRV, CAA) with
  validation and one-click templates; optional zone auto-create per site.
- **Backups**: tar.zst + mysqldump with manifests/checksums, schedules,
  retention, S3 mirroring (encrypted credentials), full/partial restores,
  daily panel self-backup.
- **Security/ops**: audit log (+ UI), JWT auth with rotating refresh tokens
  and reuse detection, login rate limiting, security headers, optional panel
  IP allowlist, panel-behind-Caddy vhost, CI-enforced structural guards
  (no shell=True, runner-only subprocess, SQL confined to one validated
  module, WP-CLI never root), 85% coverage floor on system/services, chaos
  tests, systemd self-recovery, threat model (`docs/SECURITY.md`).
- **Tooling**: one-command Multipass dev VM, production installer /
  update / uninstall scripts, Playwright E2E suite (VM-bound), Dependabot.

### Known gaps (tracked for v1.0.0 final)
- All VM-bound verification: E2E runs, installer on clean VM (×2 for
  idempotency), restore drill, ZAP scan, Lighthouse ≥95, real-phone pass,
  API p95 measurement, Filebrowser upload-ownership hook.
