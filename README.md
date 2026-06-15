# HostyPanel

HostyPanel is a fast, self-hosted deployment platform for Ubuntu 24.04. It covers
traditional web hosting (PHP sites, WordPress, DNS, databases) and modern container
stack deployments — think Coolify without Docker, built on Podman Quadlet and systemd.

The internal technical name is `hosty`; service units, paths, and environment
variables use the `hosty` / `HOSTY_*` names.

**Status:** `v2.0.0-rc.1`. See [CHANGELOG.md](CHANGELOG.md) for release notes and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design decisions.

---

## Features

### Container Stacks (v2)

Deploy any containerised application from a Git repo or a public image — the same
way Coolify does, but running on Podman Quadlet + systemd instead of Docker Compose.

- **Git-based builds** via Dockerfile or Nixpacks. Push to trigger a rebuild;
  webhook filtering with per-stack glob watch paths limits rebuilds to relevant file changes.
- **GitHub App integration** — link a GitHub App source for automatic redeployment
  on push without exposing personal tokens.
- **Coolify-style magic env vars** — `SERVICE_FQDN_<SERVICE>` and
  `SERVICE_URL_<SERVICE>` are injected into every container automatically, updated
  whenever a domain changes, and bi-directionally synced with user env vars.
- **Per-service domain routing** through Caddy with automatic HTTPS.
- **Build layer caching** — Dockerfile builds pass `--cache-from` to reuse layers
  from the previous image; Nixpacks builds persist the Nix store between deploys.
- **Parallel service deploys** — independent services in a stack start concurrently
  via `asyncio.gather`; systemd `After=` directives handle dependency ordering automatically.
- **Zero-downtime (blue-green) deploys** — when enabled, a candidate container
  starts on a temporary port, passes health checks, Caddy's upstream is hot-swapped
  via the Admin API (no full reload), the old container is stopped, the Quadlet unit
  restarts on the normal port, and the candidate is cleaned up. No visible gap.
- **HTTP health checks** — renders Podman Quadlet `HealthCmd=/HealthInterval=/HealthRetries=`
  directives; containers are restarted automatically on failure.
- **Post-start commands** — shell commands run inside the container after startup
  (e.g. database migrations), rendered as `ExecStartPost=` in the systemd unit.
- **Real-time log streaming** — deployment logs stream live via Server-Sent Events;
  the frontend connects with a JWT query parameter (EventSource workaround).
- **Image-based rollback** — every build tags `hosty/<stack>-<service>:gen-<N>`;
  one-click rollback retags the selected image as `:latest` and restarts the container,
  no rebuild required.
- **Pending-changes banner** — the UI shows a redeploy prompt whenever
  `generation != observed_generation`.
- **Resource limits** — per-service memory and CPU cap, rendered as Podman
  `--memory` and `--cpus` flags.
- **Persistent volumes** — bind-mount directories under the tenant's home, with
  automatic `:U` ownership fix for rootless images that drop privileges.
- **Remote server support** — deploy stacks to any SSH-accessible server; the
  reconciler connects once per plan and routes all I/O through the session.
- **Convergence reconciler** — a `spec_hash` fingerprint per service detects drift;
  only services whose spec changed are restarted.

### Traditional Web Hosting (v1)

- Site provisioning with Caddy vhosts, automatic HTTPS, isolated Linux users,
  and per-site web roots.
- PHP-FPM 8.2, 8.3, and 8.4 pools with per-site version switching.
- One-click WordPress install, updates, maintenance mode, salt rotation, and
  one-time admin login links.
- MariaDB database management with show-once credentials and Adminer behind an
  authenticated panel proxy.
- Per-site Filebrowser sessions scoped to the selected site.
- Authoritative DNS through PowerDNS, including common record templates.
- Scheduled local and S3 backups with restore support.
- Outgoing email (SMTP) for temporary passwords and panel alerts, with
  verify-on-save, send-test, and IPv4-first delivery.
- Panel-on-domain HTTPS with one-click DNS record creation.

### Platform

- Two-factor authentication and active-session management.
- Audit log, dark mode, responsive UI, and keyboard-accessible workflows.
- Cloudflare-aware TLS (internal origin certificates when behind the proxy).

---

## Production Install

Target: a fresh Ubuntu 24.04 server with at least 2 GB RAM and ports 80/443
available.

```bash
curl -fsSL https://raw.githubusercontent.com/mockydevs/hosty/main/installer/install.sh | sudo bash
```

The installer prints the panel URL when it finishes. On first visit, create the
admin account.

To serve the panel itself through Caddy with HTTPS, set `HOSTY_PANEL_DOMAIN`
before running the installer:

```bash
export HOSTY_PANEL_DOMAIN=panel.example.com
curl -fsSL https://raw.githubusercontent.com/mockydevs/hosty/main/installer/install.sh | sudo bash
```

Optional install-time settings:

| Variable | Description |
|---|---|
| `HOSTY_PANEL_DOMAIN` | Serve the panel on this domain with automatic HTTPS |
| `HOSTY_PANEL_ALLOWED_IPS` | Comma-separated IP allowlist for panel access |
| `HOSTY_REPO_URL` | Alternate Git repository |
| `HOSTY_REF` | Branch, tag, or commit to install |

### Update

```bash
sudo bash /opt/hosty/installer/update.sh
```

### Uninstall

```bash
sudo bash /opt/hosty/installer/uninstall.sh
```

---

## Deploying a Stack

1. Create a stack from the **Stacks** page and choose a blueprint (e.g. `git`).
2. Set the repository URL and branch under **Configuration → Git Source**.
3. Add domains under **Configuration → General** — each service gets its own domain.
   `SERVICE_FQDN_<SERVICE>` env vars are injected automatically.
4. Click **Deploy**. The panel runs git-sync → build → container start and streams
   logs live.
5. On subsequent pushes, the GitHub App webhook or manual **Redeploy** triggers a
   fresh build. Only services whose `spec_hash` changed are restarted.

### Optional per-service settings (Advanced tab)

| Setting | What it does |
|---|---|
| **Zero-downtime deploy** | Blue-green swap — no gap for end users on restart |
| **HTTP health check** | Podman probes the container; restarts it if unhealthy |
| **Post-start command** | Shell command run inside the container after startup |
| **Resource limits** | Memory cap and CPU share |

### Watch paths (Git Source tab)

Limit rebuilds to pushes that touch specific files:

```
src/**
Dockerfile
package.json
```

Leave empty to rebuild on every push.

---

## Dev VM

The recommended development path is the Multipass VM. It runs the production
services inside Ubuntu 24.04 and mounts this repository into
`/home/ubuntu/hosty`, so host edits are live in the VM.

Requirements:

- Multipass from <https://canonical.com/multipass>
- Windows PowerShell 5.1+ on Windows, or Bash on macOS/Linux

```powershell
# Windows
cd C:\Users\Master\Code\hosty
.\installer\dev-vm\dev-vm.ps1 up
```

```bash
# macOS / Linux
cd /path/to/hosty
installer/dev-vm/dev-vm.sh up
```

`up` launches `hosty-dev` with Ubuntu 24.04, 2 CPU, 4 GB RAM, and a 20 GB disk,
runs `installer/provision.sh`, and prints the VM IP address.

Common VM commands:

```text
up         create/start, mount the repo, provision, and set up the backend
provision  rerun provisioning and backend setup
backend    run the API inside the VM at http://<vm-ip>:8800
test       run VM-bound integration tests
smoke      create a Caddy vhost plus PHP file and curl it
shell      open a shell in the VM
ip         print the VM IP
status     show Multipass VM status
down       stop the VM
destroy    delete the VM
```

---

## Running the Frontend Locally

Start the API in the VM first:

```powershell
.\installer\dev-vm\dev-vm.ps1 backend
```

Then start Vite on the host (replace `<vm-ip>` with the address printed by `dev-vm ip`):

```powershell
cd C:\Users\Master\Code\hosty\frontend
$env:HOSTY_API_TARGET = "http://<vm-ip>:8800"
pnpm install
pnpm dev
```

```bash
cd /path/to/hosty/frontend
HOSTY_API_TARGET=http://<vm-ip>:8800 pnpm install && pnpm dev
```

Open the Vite URL (usually `http://localhost:5173`). Create the admin account on
first visit. The frontend dev server proxies `/api` to `HOSTY_API_TARGET`; see
[frontend/vite.config.ts](frontend/vite.config.ts).

---

## Backend Without VM

Useful for API-only work that doesn't need Caddy, PHP-FPM, MariaDB, or systemd.

```bash
cd backend
uv sync --all-groups
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8800
```

Open <http://localhost:8800/api/docs>. First boot requires `POST /api/auth/setup`
with a username and a password of at least 12 characters.

---

## Tests and Quality

```bash
# Backend
cd backend
uv run pytest
uv run ruff check .
uv run ruff format .

# Frontend
cd frontend
pnpm test
pnpm lint
pnpm build

# E2E (requires a live VM-backed panel)
cd e2e
pnpm install
pnpm test
```

Tests marked `vm` require the privileged Ubuntu VM and are skipped by default.
Run them through the VM driver:

```powershell
.\installer\dev-vm\dev-vm.ps1 test
```

---

## Repository Layout

```text
backend/     FastAPI API, service layer, Alembic migrations, and pytest suite
frontend/    React (Vite + Tailwind) UI and Vitest tests
installer/   Production installer, updater, uninstaller, and dev VM scripts
docs/        Architecture decisions, security model, admin guide, runbooks
e2e/         Playwright suite for a live VM-backed panel
scripts/     Repository guard and maintenance utilities
```

### Key backend packages

```text
app/api/routes/       HTTP handlers (stacks, sites, auth, …)
app/domain/           Pure domain: specs, actions, planner (no I/O)
app/orchestration/    Reconciler, executor, observer (host I/O)
app/services/         Business logic: Caddy config, stack spec builder, …
app/system/           Host abstraction (Quadlet, systemd, SSH, tenants)
app/db/               SQLAlchemy models and Alembic env
```

---

## Architecture in One Paragraph

Each containerised stack is represented as a `StackSpec` (pure data). A
`spec_hash` fingerprint per service is embedded in every Podman Quadlet unit
file. On each reconcile cycle the **observer** reads back the hashes from disk
and the running state from systemd; the **planner** diffs desired vs observed
and emits a minimal action list; the **executor** applies the actions through
a `HostContext` (local or SSH). Caddy is rebuilt from scratch at the end of
every non-empty plan via the Admin API — ingress is derived state, never a
source of truth. Zero-downtime deploys bypass the Quadlet restart and instead
run a temporary candidate container, hot-patch a single Caddy route, then
cleanly swap. The reconciler is event-driven: API writes enqueue an immediate
convergence; a background loop catches any drift.
