#!/usr/bin/env bash
# Backend setup inside the dev VM — run as root, after installer/provision.sh.
#
# Everything stateful lives OUTSIDE the host mount, in /var/lib/hosty:
# Multipass mounts don't support the file locking SQLite needs, and a venv
# on the mount would be painfully slow. The mounted repo stays source-only.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo $0)" >&2; exit 1; }

REPO="${HOSTY_REPO:-/home/ubuntu/hosty}"
VENV=/var/lib/hosty/venv
ENV_FILE=/var/lib/hosty/dev.env

log() { printf '\n==> %s\n' "$*"; }

log "uv"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

log "Backend dependencies → $VENV"
install -d -m 0755 /var/lib/hosty
cd "$REPO/backend"
UV_PROJECT_ENVIRONMENT=$VENV uv sync --all-groups

log "Dev environment file → $ENV_FILE"
if [[ ! -f $ENV_FILE ]]; then
  cat > "$ENV_FILE" <<ENV
HOSTY_ENV=dev
HOSTY_SECRET_KEY=$(openssl rand -hex 32)
HOSTY_DATABASE_URL=sqlite+aiosqlite:////var/lib/hosty/hosty.db
HOSTY_COOKIE_SECURE=false
HOSTY_CREATE_TABLES_ON_STARTUP=false
HOSTY_CADDY_TLS_INTERNAL=true
ENV
  chmod 600 "$ENV_FILE"
fi

# Append settings introduced after an existing dev.env was generated.
if ! grep -q '^HOSTY_CADDY_TLS_INTERNAL=' "$ENV_FILE"; then
  echo 'HOSTY_CADDY_TLS_INTERNAL=true' >> "$ENV_FILE"
fi

log "Database schema (alembic upgrade head)"
set -a
# shellcheck disable=SC1090  # dev.env is generated above
. "$ENV_FILE"
set +a
UV_PROJECT_ENVIRONMENT=$VENV uv run alembic upgrade head

log "Done. Start the API from the host with: dev-vm backend"
