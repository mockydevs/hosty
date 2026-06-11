# Hosty

A clean, modern, fast hosting control panel (CyberPanel-lite, no email).
FastAPI + React + Caddy on Ubuntu 24.04 LTS.

**Status:** Phase 6 complete — auth, sites, PHP/WordPress, databases (Adminer), and the per-site file manager (Filebrowser). VM-bound verification deferred until the dev VM exists.
See [TASKS.md](TASKS.md) for the full roadmap and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for decisions.

## Dev VM (one command)

The full stack (Caddy, PHP-FPM 8.2–8.4, MariaDB, PowerDNS, Filebrowser,
WP-CLI) runs in a [Multipass](https://canonical.com/multipass) VM that
mirrors production. The repo is mounted into the VM, so host edits are live.

```powershell
.\installer\dev-vm\dev-vm.ps1 up        # Windows
```

```bash
installer/dev-vm/dev-vm.sh up           # macOS / Linux
```

`up` launches `hosty-dev` (Ubuntu 24.04, 2 CPU / 4 GB / 20 GB), mounts the
repo, runs `installer/provision.sh` + backend setup, and prints the VM IP.
Then:

```text
dev-vm backend    run the API inside the VM (root, like production) → http://<vm-ip>:8800
dev-vm test       run the VM-bound integration tests (pytest -m vm)
dev-vm smoke      create a Caddy vhost + PHP file, curl it
dev-vm shell      SSH into the VM        dev-vm down|destroy  stop / delete it
```

Frontend dev server stays on the host:
`HOSTY_API_TARGET=http://<vm-ip>:8800 pnpm dev` (see `frontend/vite.config.ts`).

## Backend quickstart (no VM)

```bash
cd backend
uv sync --all-groups          # install deps (https://docs.astral.sh/uv/)
cp .env.example .env          # adjust if needed
uv run alembic upgrade head   # create the database schema
uv run uvicorn app.main:app --reload --port 8800
```

Open http://localhost:8800/api/docs for interactive API docs.

First boot: `POST /api/auth/setup` with a username and password (min 12 chars)
creates the admin account. Setup locks itself after the first user exists.

## Development

```bash
cd backend
uv run pytest                 # test suite
uv run ruff check .           # lint
uv run ruff format .          # format
```

Tests marked `vm` need a privileged Ubuntu VM and are skipped by default:
`uv run pytest -m vm` runs them.

## Repository layout

```
backend/    FastAPI API (app/) + tests
frontend/   React UI (Phase 2)
installer/  Production install scripts (Phase 10)
docs/       Architecture decisions & conventions
```
