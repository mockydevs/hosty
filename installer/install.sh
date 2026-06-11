#!/usr/bin/env bash
# Hosty production installer for a FRESH Ubuntu 24.04 server.
# Idempotent: safe to re-run. Usage:
#   curl -fsSL https://raw.githubusercontent.com/mockydevs/hosty/main/installer/install.sh | sudo bash
# or, from a clone:  sudo bash installer/install.sh
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo $0)" >&2; exit 1; }
. /etc/os-release
[[ ${VERSION_ID:-} == "24.04" ]] || echo "WARNING: tested on Ubuntu 24.04, found ${VERSION_ID:-unknown}"

REPO_URL="${HOSTY_REPO_URL:-https://github.com/mockydevs/hosty.git}"
REPO_REF="${HOSTY_REF:-main}"
APP_DIR=/opt/hosty
STATE_DIR=/var/lib/hosty
ENV_FILE=$STATE_DIR/hosty.env
VENV=$STATE_DIR/venv

log() { printf '\n==> %s\n' "$*"; }

log "Hosty source → $APP_DIR (ref: $REPO_REF)"
apt-get update -q && apt-get install -qy git
if [[ -d $APP_DIR/.git ]]; then
  git -C "$APP_DIR" fetch --tags origin
  git -C "$APP_DIR" checkout -q "$REPO_REF"
  git -C "$APP_DIR" pull -q --ff-only origin "$REPO_REF" 2>/dev/null || true
else
  git clone --branch "$REPO_REF" "$REPO_URL" "$APP_DIR"
fi

log "Stack (Caddy, PHP, MariaDB, PowerDNS, Filebrowser, Adminer, WP-CLI)"
bash "$APP_DIR/installer/provision.sh"

log "uv"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

log "Backend dependencies → $VENV"
install -d -m 0750 "$STATE_DIR"
(cd "$APP_DIR/backend" && UV_PROJECT_ENVIRONMENT=$VENV uv sync)

log "Frontend build"
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -qy nodejs
fi
corepack enable

# Helper: build the frontend with fallbacks for pnpm v10 build-script approval.
build_frontend() {
  cd "$APP_DIR/frontend"

  # Strategy 1: normal install (works when lockfile already has approvals).
  if pnpm install 2>/dev/null; then
    pnpm exec vite build && return 0
  fi

  log "Retrying frontend install with clean node_modules..."
  # Strategy 2: wipe cached state so pnpm re-reads pnpm-workspace.yaml.
  rm -rf node_modules .pnpm-store
  if pnpm install 2>/dev/null; then
    pnpm exec vite build && return 0
  fi

  log "Retrying with --ignore-scripts + manual rebuild..."
  # Strategy 3: skip all scripts during install, then rebuild the two packages
  # that need native binaries.
  rm -rf node_modules
  pnpm install --ignore-scripts
  pnpm rebuild @biomejs/biome esbuild 2>/dev/null || true
  pnpm exec vite build && return 0

  echo "ERROR: Frontend build failed after all attempts." >&2
  return 1
}
build_frontend

log "Configuration → $ENV_FILE"
PANEL_DOMAIN="${HOSTY_PANEL_DOMAIN:-}"
if [[ ! -f $ENV_FILE ]]; then
  cat > "$ENV_FILE" <<ENV
HOSTY_ENV=prod
HOSTY_SECRET_KEY=$(openssl rand -hex 32)
HOSTY_DATABASE_URL=sqlite+aiosqlite:///$STATE_DIR/hosty.db
HOSTY_COOKIE_SECURE=true
HOSTY_CREATE_TABLES_ON_STARTUP=false
HOSTY_FRONTEND_DIST=$APP_DIR/frontend/dist
ENV
  [[ -n $PANEL_DOMAIN ]] && echo "HOSTY_PANEL_DOMAIN=$PANEL_DOMAIN" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

log "Database schema (alembic upgrade head)"
(cd "$APP_DIR/backend" \
  && set -a && . "$ENV_FILE" && set +a \
  && UV_PROJECT_ENVIRONMENT=$VENV uv run alembic upgrade head)

log "systemd unit"
install -m 0644 "$APP_DIR/installer/systemd/hosty.service" /etc/systemd/system/hosty.service
systemctl daemon-reload
systemctl enable --now hosty
systemctl restart hosty

IP=$(hostname -I | awk '{print $1}')
log "Done."
cat <<MSG

  Hosty is running.

  Panel:     http://${PANEL_DOMAIN:-$IP:8800}  (HTTPS via Caddy once HOSTY_PANEL_DOMAIN
             is set and the domain points at this server)
  First run: open the panel and create the admin account (first-boot setup).
  Update:    sudo bash $APP_DIR/installer/update.sh
  Uninstall: sudo bash $APP_DIR/installer/uninstall.sh

MSG
