# Hosty v2 — Container-Native Rearchitecture: Working Plan

**Authority:** ADR-013 in [ARCHITECTURE.md](ARCHITECTURE.md). Milestone
checklists live in [TASKS.md](../TASKS.md). This document is the detailed
companion: exact modules, schemas, semantics, and test plans per milestone,
so any session can resume any milestone cold.

**Prime directive:** every milestone ships END TO END — real code, real
tests, VM-verified where it touches the host. No placeholders, no stubs
left behind, no "TODO later". A milestone is done when its gate (below)
passes, and not before.

---

## 0. Non-negotiable quality principles (apply to every milestone)

1. **Pure core.** `domain/` imports nothing that does I/O. The planner is a
   pure function; if a piece of logic is hard, it belongs here where it can
   be exhaustively tested.
2. **One seam per external system** (`system/` modules), argv-built via
   `runner.run` — never a shell, never an SDK. Every user-influenced argv
   value passes an allowlist grammar that rejects a leading `-`
   (option-injection) by construction.
3. **ORM-free specs.** Frozen dataclasses cross layer boundaries; SQLAlchemy
   models never leave the service layer.
4. **Tests are the spec.** Pure functions: exhaustive unit + property-based
   (hypothesis) tests. Adapters: argv/unit-text snapshot tests + injection
   cases. API: behavior tests with fully faked system layer (FakePodman,
   pattern from `tests/test_apps_api.py`). Host-touching code: `vm`-marked
   integration tests run via `dev-vm.ps1 test`.
5. **Tenancy rules:** clients get 404-not-403 for others' resources;
   secrets are show-once + Fernet at rest (`core/secrets.py`); request
   bodies never logged; quotas enforced at create.
6. **Gate for every milestone:** `uv run pytest` green, `uv run ruff check`
   + `format --check` clean, frontend `pnpm test && pnpm lint && pnpm build`
   green when UI changed, installer security tests updated when the
   provisioner changed, CHANGELOG + TASKS.md updated, no unreferenced TODO.

## 1. Target module layout

```
backend/app/
  domain/                 NEW — pure, zero I/O
    specs.py              StackSpec, ServiceSpec, VolumeSpec, EndpointSpec,
                          ObservedStack, ObservedService (frozen dataclasses)
    validate.py           grammar: image refs, env keys, mount paths, names
                          (salvaged + extended from system/docker.py)
    actions.py            Action union (frozen dataclasses, see M1)
    planner.py            plan(desired, observed) -> list[Action]
  system/                 host adapters (existing home: runner, fs, systemd)
    tenants.py            NEW — Linux user per client: useradd/subuids/linger
    quadlet.py            NEW — spec -> .container/.network/.volume unit text
    systemd_user.py       NEW — systemctl --machine <user>@.host --user ...
    podman.py             NEW — observe (ps/inspect via per-user socket),
                          exec (blueprint actions), image digest resolve
  orchestration/          NEW
    reconciler.py         converge loop + per-stack serialization
    executor.py           Action -> adapter dispatch
    operations.py         op-step recording (extracted from services/sites.py)
    blueprints/
      base.py             Blueprint protocol + registry
      raw_image.py        Blueprint #0
      wordpress.py        Blueprint #1 (M5)
  services/               business layer (kept) + ports.py allocator
  api/routes/stacks.py    NEW — replaces sites.py + apps.py at M6
frontend/src/pages/stacks*.tsx (M4)
```

## 2. Database schema (introduced M1/M4, native tables dropped M6)

```
tenants:          user_id PK/FK(users), linux_user UNIQUE, uid, subuid_start,
                  subuid_count, created_at
stacks:           id, owner_id FK(users, SET NULL), name UNIQUE (slug),
                  blueprint_id, blueprint_version, inputs_encrypted (Fernet
                  JSON of user inputs incl. secrets), status (converging|
                  ready|degraded|suspended|deleting|error), error_message,
                  generation INT (bumped on every spec edit),
                  observed_generation INT, created_at, updated_at
stack_services:   id, stack_id FK CASCADE, name, image, image_digest,
                  internal_port?, host_port? UNIQUE, env_encrypted,
                  memory_mb?, cpu_percent?, is_web BOOL
stack_volumes:    id, stack_id FK CASCADE, name, service_name, mount_path
stack_endpoints:  id, stack_id FK CASCADE, domain UNIQUE, service_name,
                  behind_cloudflare BOOL
operations:       + stack_id FK nullable (kinds: create_stack, delete_stack,
                  converge_stack, action:<blueprint>.<action>)
users/plans:      + max_stacks (max_sites/max_apps dropped at M6)
```

Generation semantics (K8s-style): API writes bump `generation`; the
reconciler sets `observed_generation = generation` only after convergence.
`status` is a cached projection for the UI, never an input to planning.

---

## M0 — Host foundation (provisioner + tenant manager + VM spike)

**Goal:** prove every risky mechanism on the dev VM before core code exists.

Deliverables:
- `installer/provision.sh`: install `podman uidmap passt crun` (Ubuntu 24.04
  archive versions; record them in the "Done. Versions" block). Docker
  section stays until M6 (Apps MVP still references it).
- `backend/app/system/tenants.py`: argv builders + async ops —
  `ensure_tenant(user_id) -> TenantInfo`: `useradd -m -s /usr/sbin/nologin
  hosty-t-<id>`, explicit `usermod --add-subuids/--add-subgids` from a
  panel-managed range ledger (tenants table), `loginctl enable-linger`,
  slice attach via existing `system/slices.py`. `remove_tenant` reverses.
  Tests: argv snapshots, name grammar (`hosty-t-<int>` only), idempotency.
- Migration `0014_tenants`.
- `installer/dev-vm/spike-quadlet.sh` (kept in repo as executable
  documentation): writes a hand-rolled nginx `.container` quadlet into
  `/etc/containers/systemd/users/<uid>/`, `systemctl --machine
  hosty-t-X@.host --user daemon-reload && start`, asserts: rootless process
  uid, `127.0.0.1:<port>` answering, Caddy route proxying it, journald logs
  visible via `journalctl --user-unit -M hosty-t-X@`, memory cap from the
  user slice honored, survives reboot (linger).
- Record spike findings as comments in the script (pasta vs slirp4netns
  performance, any image quirks) — these decide M2 defaults.

**Gate:** spike passes on the VM end-to-end; tenants tests green.

## M1 — Domain core (pure)

**Goal:** the entire lifecycle expressed as data + a planner that is the
most-tested code in the repo.

Deliverables:
- `domain/specs.py`. StackSpec carries: name, tenant linux_user, services
  (each: name, image **digest-pinned**, env (plain dict — encryption is a
  storage concern), internal/host ports, volumes mounted, limits, is_web),
  network name (`hosty-<stack>`), endpoints. ObservedStack mirrors it from
  the host's perspective (unit files present?, unit active?, container
  digest running?, port bound?).
- `domain/actions.py` — the complete Action union:
  `EnsureTenant, EnsureVolumeDir, WriteUnits (full desired unit-file set),
  RemoveUnits, DaemonReload, StartService, StopService, RestartService,
  RemoveVolumeDir, RemoveTenantIfEmpty, SyncCaddy` — each a frozen
  dataclass with everything the executor needs; ordering is the planner's
  job, actions are dumb.
- `domain/planner.py` — `plan(desired: list[StackSpec], observed:
  Observed) -> list[Action]`. Covers: fresh create, delete, image/env/port
  change (unit rewrite + restart), suspension (scale-to-zero: stop services,
  keep volumes, SyncCaddy 503), resume, drift repair (unit missing/stopped/
  wrong digest), no-op when converged (MUST return []).
- `domain/validate.py` — grammar moved out of `system/docker.py` (docker.py
  keeps importing from here until its M6 deletion; one source of truth).
- Add `hypothesis` to dev deps. Property tests: for arbitrary
  (desired, observed) pairs, simulating `apply(plan(...))` over a model
  host yields observed == desired, and `plan(desired, simulate(...)) == []`
  (convergence + idempotency — the two theorems the whole system rests on).

**Gate:** planner branch-coverage at 100% (enforce via
`pytest --cov=app.domain --cov-fail-under=100` in CI for this package);
property suites green.

## M2 — Adapters

**Goal:** thin, snapshot-tested bridges; a StackSpec round-trips on the VM.

Deliverables:
- `system/quadlet.py`: pure renderers `container_unit(spec) -> str`,
  `network_unit`, `volume_dir layout`. Hardening lines emitted ALWAYS:
  `NoNewPrivileges=true`, `PublishPort=127.0.0.1:<host>:<internal>`,
  pinned digest image, `EnvironmentFile=` (0600, tenant-owned, written via
  fs seam — never env in unit text), log namespace defaults, labels
  `hosty.stack=<name>`. Placement: `/etc/containers/systemd/users/<uid>/`
  (root-managed user units; podman ≥ 4.9 on 24.04). Snapshot tests for every
  variant + injection attempts.
- `system/systemd_user.py`: argv builders for `systemctl --machine
  <user>@.host --user {daemon-reload,start,stop,restart,show,is-active}`
  and `journalctl -M <user>@ --user-unit <unit> -n <tail>`. Unit-name
  grammar enforced. Graceful degradation contract identical to
  `system/systemd.py`.
- `system/podman.py`: observe via the tenant's user socket
  (`podman --url unix:/run/user/<uid>/podman/podman.sock ps/inspect
  --format json` — quadlet ships `podman.socket` enabled per tenant in M0
  spike-verified form), `exec` for blueprint actions, `image digest`
  resolution. Parsers total (never raise on garbage) — pattern from
  the 12a `parse_state`.
- `services/ports.py`: loopback allocator on `stack_services.host_port`
  (DB-ledger, UNIQUE-race-safe — proven pattern).
- `services/caddy.py`: `EndpointSpec` route building (reverse_proxy to
  `127.0.0.1:<host_port>`, suspension 503, Cloudflare internal-TLS) joins
  `build_config`; Site/App specs remain until M6.
- New `vm`-marked integration test: create spec → render → place → reload →
  start → assert serving → tear down, on the dev VM.

**Gate:** snapshot + injection suites green; the VM round-trip test passes.

## M3 — Orchestration

**Goal:** the reconciler is the only writer to the host; drift heals.

Deliverables:
- `orchestration/executor.py`: Action → adapter dispatch, sequential per
  stack, structured logging per action.
- `orchestration/reconciler.py`: converge-on-startup (like Caddy republish)
  + interval loop (setting `reconcile_interval_seconds`, default 60) +
  on-demand `converge(stack_id)` after API writes. Per-stack asyncio locks;
  global concurrency cap. Suspended owners → planner sees `suspend=True`.
- `orchestration/operations.py`: extract `_Step`/`_run_pipeline`-equivalent
  recording from `services/sites.py` WITHOUT touching sites.py behavior;
  operations now record the planner's emitted actions as steps (UI contract
  unchanged: name/label/status list).
- Drift notifications: persistent divergence across N cycles (default 3) →
  Notification with `dedupe_key=stack:<id>:drift`; resolves on convergence.
- Failure semantics: an action failure marks the operation step failed,
  stack `status=degraded|error`, and the NEXT cycle retries — no
  compensating undo anywhere.

**Gate:** unit tests with FakeHost (kill a container between cycles →
next cycle replans + restarts + notifies); `vm` test: `podman stop` by hand
on the VM, panel converges within one interval.

## M4 — Blueprint engine + Stacks API/UI

**Goal:** a client deploys a Next.js image with domain + HTTPS from the UI.

Deliverables:
- `blueprints/base.py`: `class Blueprint(Protocol)`: `id`, `version`,
  `inputs() -> pydantic model` (drives the UI form: typed fields, secret
  flags, defaults), `render(inputs, alloc) -> StackSpec` (alloc = ports,
  generated secrets, tenant), `actions() -> dict[str, ActionHandler]`,
  `backup_hooks() -> BackupHooks | None`, `health(observed) -> StackHealth`.
  Registry with explicit registration; blueprint version recorded on the
  stack (a blueprint change never silently mutates existing stacks —
  re-render happens only on explicit "upgrade" action).
- `blueprints/raw_image.py`: inputs = image, internal_port, domain, env
  map, volume list, limits. Render → single-service StackSpec.
- `api/routes/stacks.py`: list/get/create/delete (202 + operation),
  `POST /{id}/actions/{action}` (typed result envelope, show-once
  payloads supported), `GET /{id}/logs?service=&tail=` (journald via
  systemd_user), suspend-resume inherited from owner state. Owner-scoped
  404s, `max_stacks` quota (users/plans/quotas + admin routes wiring —
  same shape as the max_apps work, which it replaces at M6).
- Frontend: `stacks.tsx` (list, status pills driven by
  generation/observed_generation), `stack-create.tsx` (wizard rendered FROM
  the blueprint inputs schema — no per-blueprint UI code), `stack-detail.tsx`
  (services, endpoints, logs viewer, actions with confirm + show-once
  modal — reuse DB-credentials pattern). OpenAPI types regenerated; Vitest
  for the schema-driven form renderer.
- Migration `0015_stacks` (schema §2).

**Gate:** full-suite green incl. new FakePodman-backed API tests (happy,
rollback-free failure → degraded, quota, 404-scoping, action dispatch,
logs); e2e on VM: raw-image stack reachable over HTTPS.

## M5 — WordPress blueprint (parity is the definition of done)

**Goal:** one-click WordPress, fully containerized, every v1 convenience.

Deliverables:
- `blueprints/wordpress.py`: services `web` = `wordpress:<php>-apache`
  (digest-pinned per supported PHP series) + `db` = `mariadb:<ver>`
  (per-stack, stack-network-internal, no host port) + WP-CLI via the
  `wordpress:cli` image (`podman run --rm` inside the stack network,
  sharing the web volumes) behind `system/podman.py`. Volumes: `wp-content`
  (+ `db-data`). Generated secrets: DB password, WP salts, admin password —
  show-once.
- Actions (parity with `services/wordpress.py` v1): `install` (post-create
  operation), `core_update`, `plugins_update`, `themes_update`,
  `maintenance_on/off`, `rotate_salts`, `admin_login_link` (one-time, same
  TTL semantics), `status` (versions, update counts, health).
- Backups: BackupHooks = `pre: mysqldump via podman exec` + volume dirs →
  existing ADR-010 directory format; restore path implemented and tested
  (restore = stop stack, restore volumes + dump import, start).
- Per-stack Adminer/Filebrowser: ticket-proxy targets re-pointed at stack
  DB/volume (extend existing proxies — contract unchanged for the UI).
- PHP version switch = blueprint upgrade action (new image digest, planner
  restarts web — zero-downtime not required for v1 parity).

**Gate:** a parity test file mirroring every v1 WordPress API test against
the blueprint (FakePodman); VM e2e: create → install → login link works →
update → backup → destroy → restore-into-new-stack serves the same content.

## M6 — The purge

**Goal:** one workload system remains. `grep -r php_fpm backend/` → nothing.

Deliverables:
- Delete: `services/sites.py` provisioning + `php_fpm.py` + `wordpress.py`
  + `staging.py` + `site_import.py` + `mariadb.py` host-mode +
  `system/docker.py` + `services/apps.py` + apps/sites routes + Site/App
  models & UI pages. Re-home keepers first: domain validation →
  `domain/validate.py`; op recording already in orchestration (M3);
  `build_full_config` ownership moves to a slim `services/ingress.py`.
- Provisioner: remove PHP PPA/pools, host MariaDB, WP-CLI, host
  Adminer/Filebrowser, Docker engine; `installer/install-tools.sh` shrinks
  accordingly; installer security tests updated to assert the REMOVALS.
- Migration `0016_v2_purge`: drop sites/apps/databases(host)/related
  columns (max_sites/max_apps → max_stacks only).
- Docs sweep: README, ADMIN-GUIDE, RUNBOOK-RESTORE, SECURITY threat model,
  CHANGELOG; version bump to v2.0.0.
- Staging & site-import return post-v2 as blueprint features (clone-stack:
  copy volumes + dump/restore; importer: blueprint that ingests an archive)
  — tracked in TASKS.md backlog, NOT in scope for M6.

**Gate:** full suite green; fresh VM install hosts WordPress + a raw-image
Django side by side, all containers, rootless; no dead code (`ruff` +
grep sweeps for deleted module names).

---

## Session bootstrap (read this first next time)

1. Read this file, then ADR-013, then the v2 section of TASKS.md for
   current checkbox state.
2. `git log --oneline -10` to see where work stopped; run the full backend
   suite before writing anything.
3. Work the lowest unfinished milestone IN ORDER; milestones are strictly
   sequential (M2 needs M1's specs; M3 needs M2's adapters; …).
4. Update TASKS.md checkboxes and CHANGELOG as part of the work, not after.
