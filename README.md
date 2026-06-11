# Hosty

A clean, modern, fast hosting control panel (CyberPanel-lite, no email).
FastAPI + React + Caddy on Ubuntu 24.04 LTS.

**Status:** Phase 1 complete — backend core (auth, system layer, health/system APIs).
See [TASKS.md](TASKS.md) for the full roadmap and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for decisions.

## Backend quickstart

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
