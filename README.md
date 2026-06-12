# HostyPanel

HostyPanel is a clean, fast hosting control panel for Ubuntu 24.04.
It combines a FastAPI backend, a React frontend, and a Caddy-managed hosting
stack. The internal technical name is `hosty`; service units, paths, and
environment variables use the `hosty` / `HOSTY_*` names.

**Status:** `v1.0.0-rc.1`. The feature set is complete and the final
on-VM verification round is in progress. See [TASKS.md](TASKS.md) for the
roadmap, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design decisions,
and [CHANGELOG.md](CHANGELOG.md) for release notes.

## Features

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
  verify-on-save, send-test, and IPv4-first delivery (optional IPv6).
- Panel-on-domain HTTPS with one-click DNS record creation for the panel
  domain when the zone is hosted on the panel itself.
- Two-factor authentication and active-session management.
- Audit log, dark mode, responsive UI, and keyboard-accessible workflows.

Screenshots are tracked under `docs/screenshots/` during VM verification.

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

- `HOSTY_PANEL_ALLOWED_IPS`: comma-separated allowlist for panel access.
- `HOSTY_REPO_URL`: alternate Git repository.
- `HOSTY_REF`: branch, tag, or commit to install.

### Update Hosty

To update to the latest version of Hosty, run the following command:

```bash
sudo bash /opt/hosty/installer/update.sh
```

### Uninstall Hosty

To uninstall Hosty, run the following command:

```bash
sudo bash /opt/hosty/installer/uninstall.sh
```

## Dev VM

The recommended development path is the Multipass VM. It runs the production
services inside Ubuntu 24.04 and mounts this repository into
`/home/ubuntu/hosty`, so host edits are live in the VM.

Requirements:

- Multipass installed from <https://canonical.com/multipass>
- Windows PowerShell 5.1+ on Windows, or Bash on macOS/Linux

Start or reprovision the VM:

```powershell
cd C:\Users\Master\Code\hosty
.\installer\dev-vm\dev-vm.ps1 up
```

```bash
cd /path/to/hosty
installer/dev-vm/dev-vm.sh up
```

`up` launches `hosty-dev` with Ubuntu 24.04, 2 CPU, 4 GB RAM, and a 20 GB disk.
It then runs `installer/provision.sh`, performs backend setup, and prints the
VM IP address.

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

Examples:

```powershell
cd C:\Users\Master\Code\hosty
.\installer\dev-vm\dev-vm.ps1 backend
.\installer\dev-vm\dev-vm.ps1 test
```

```bash
cd /path/to/hosty
installer/dev-vm/dev-vm.sh backend
installer/dev-vm/dev-vm.sh test
```

## Test In The Browser

After `dev-vm up` finishes, it prints a VM IP address such as
`172.17.74.67`. Use that IP in the commands below.

Open a new PowerShell window, go to the repository root, and start the backend
inside the VM:

```powershell
cd C:\Users\Master\Code\hosty
.\installer\dev-vm\dev-vm.ps1 backend
```

Keep that terminal running. Then open the backend API docs in a browser:

```text
http://<vm-ip>:8800/api/docs
```

Example:

```text
http://172.17.74.67:8800/api/docs
```

To test the full web panel, open another PowerShell window and run the frontend
from the host machine:

```powershell
cd C:\Users\Master\Code\hosty\frontend
$env:HOSTY_API_TARGET = "http://<vm-ip>:8800"
pnpm install
pnpm dev
```

Example:

```powershell
cd C:\Users\Master\Code\hosty\frontend
$env:HOSTY_API_TARGET = "http://172.17.74.67:8800"
pnpm install
pnpm dev
```

Open the Vite URL printed by `pnpm dev`, usually:

```text
http://localhost:5173
```

Create the admin account on first visit. Keep both terminals running while
testing: one for `dev-vm backend`, and one for `pnpm dev`.

## Frontend Development

Run the API in the VM first:

```powershell
cd C:\Users\Master\Code\hosty
.\installer\dev-vm\dev-vm.ps1 backend
```

Then start Vite on the host. Replace `<vm-ip>` with the value printed by
`dev-vm ip`.

```powershell
cd C:\Users\Master\Code\hosty\frontend
$env:HOSTY_API_TARGET = "http://<vm-ip>:8800"
pnpm install
pnpm dev
```

```bash
cd /path/to/hosty/frontend
pnpm install
HOSTY_API_TARGET=http://<vm-ip>:8800 pnpm dev
```

The frontend dev server proxies `/api` to `HOSTY_API_TARGET`; see
[frontend/vite.config.ts](frontend/vite.config.ts).

## Backend Quickstart Without VM

This path is useful for API-only work that does not need Caddy, PHP-FPM,
MariaDB, PowerDNS, Filebrowser, or systemd integration.

```bash
cd backend
uv sync --all-groups
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8800
```

Open <http://localhost:8800/api/docs> for the interactive API docs.

First boot requires `POST /api/auth/setup` with a username and a password of at
least 12 characters. Setup locks after the first user exists.

## Test And Quality Commands

Backend:

```bash
cd backend
uv run pytest
uv run ruff check .
uv run ruff format .
```

Frontend:

```bash
cd frontend
pnpm test
pnpm lint
pnpm build
```

E2E tests run against a live VM-backed panel:

```bash
cd e2e
pnpm install
pnpm test
```

Tests marked `vm` require the privileged Ubuntu VM and are skipped by default in
the normal backend test command. Run them through the VM driver:

```powershell
.\installer\dev-vm\dev-vm.ps1 test
```

```bash
installer/dev-vm/dev-vm.sh test
```

## Repository Layout

```text
backend/     FastAPI API, service layer, migrations, and pytest suite
frontend/    React UI, generated OpenAPI types, and Vitest tests
installer/   Production installer, updater, uninstaller, and dev VM driver
docs/        Architecture, security model, admin guide, and runbooks
e2e/         Playwright suite for a live VM-backed panel
scripts/     Repository guard and maintenance scripts
```
