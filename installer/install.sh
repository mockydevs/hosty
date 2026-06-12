#!/usr/bin/env bash
# Hosty production installer for a FRESH Ubuntu 24.04 server.
# Idempotent: safe to re-run. Usage:
#   curl -fsSL https://raw.githubusercontent.com/mockydevs/hosty/main/installer/install.sh | sudo bash
# or, from a clone:  sudo bash installer/install.sh
set -euo pipefail

# The caller may be sitting inside a directory this script replaces (e.g.
# /opt/hosty); a deleted cwd breaks getcwd for every child process.
cd /

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
  # Deploy checkout: force it to match the remote (a swallowed pull here once
  # made re-installs silently keep old code).
  if git -C "$APP_DIR" rev-parse -q --verify "origin/$REPO_REF" >/dev/null 2>&1; then
    git -C "$APP_DIR" reset --hard "origin/$REPO_REF"
  fi
else
  git clone --branch "$REPO_REF" "$REPO_URL" "$APP_DIR"
fi

log "Stack (Caddy, PHP, MariaDB, PowerDNS, Filebrowser, Adminer, WP-CLI)"
bash "$APP_DIR/installer/provision.sh"

log "uv"
if ! command -v uv >/dev/null; then
  bash "$APP_DIR/installer/install-tools.sh" uv
fi

log "Backend dependencies → $VENV"
install -d -m 0750 "$STATE_DIR"
(cd "$APP_DIR/backend" && UV_PROJECT_ENVIRONMENT=$VENV uv sync --frozen)

log "Frontend build"
if ! command -v node >/dev/null; then
  bash "$APP_DIR/installer/install-tools.sh" node
fi
# pnpm version is pinned via "packageManager" in frontend/package.json;
# corepack fetches exactly that version (no floating to latest).
# Build-script approvals live in frontend/pnpm-workspace.yaml (allowBuilds).
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
corepack enable

(
  cd "$APP_DIR/frontend"
  pnpm install --frozen-lockfile
  pnpm exec vite build
)

log "Configuration → $ENV_FILE"
PANEL_DOMAIN="${HOSTY_PANEL_DOMAIN:-}"
PANEL_ALLOWED_IPS="${HOSTY_PANEL_ALLOWED_IPS:-}"
# Public IPv4: enables DNS "point to this server" records and templates.
# Override with HOSTY_PUBLIC_IP=... ; detection: external echo, then first
# local address as a fallback (fine for VMs with a public primary interface).
PUBLIC_IP="${HOSTY_PUBLIC_IP:-}"
if [[ -z $PUBLIC_IP ]]; then
  PUBLIC_IP=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || true)
fi
if [[ -z $PUBLIC_IP ]]; then
  PUBLIC_IP=$(hostname -I | awk '{print $1}')
fi
if [[ ! -f $ENV_FILE ]]; then
  cat > "$ENV_FILE" <<ENV
HOSTY_ENV=prod
HOSTY_SECRET_KEY=$(openssl rand -hex 32)
HOSTY_DATABASE_URL=sqlite+aiosqlite:///$STATE_DIR/hosty.db
# Secure cookies only work over HTTPS. Without a panel domain the panel is
# reached over plain http://IP:8800, where the browser would drop the session
# cookie (instant logouts). Set this to true once HOSTY_PANEL_DOMAIN is live.
HOSTY_COOKIE_SECURE=$([[ -n $PANEL_DOMAIN ]] && echo true || echo false)
HOSTY_CREATE_TABLES_ON_STARTUP=false
HOSTY_FRONTEND_DIST=$APP_DIR/frontend/dist
HOSTY_PDNS_API_KEY=$(cat /etc/hosty/pdns-api-key 2>/dev/null || echo "")
HOSTY_PUBLIC_IP=$PUBLIC_IP
ENV
  [[ -n $PANEL_DOMAIN ]] && echo "HOSTY_PANEL_DOMAIN=$PANEL_DOMAIN" >> "$ENV_FILE"
  [[ -n $PANEL_ALLOWED_IPS ]] && echo "HOSTY_PANEL_ALLOWED_IPS=$PANEL_ALLOWED_IPS" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
elif ! grep -q '^HOSTY_PUBLIC_IP=' "$ENV_FILE"; then
  # Idempotent upgrade path: older installs predate HOSTY_PUBLIC_IP.
  echo "HOSTY_PUBLIC_IP=$PUBLIC_IP" >> "$ENV_FILE"
fi
if [[ -n $PANEL_ALLOWED_IPS ]]; then
  sed -i '/^HOSTY_PANEL_ALLOWED_IPS=/d' "$ENV_FILE"
  echo "HOSTY_PANEL_ALLOWED_IPS=$PANEL_ALLOWED_IPS" >> "$ENV_FILE"
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

IP=$PUBLIC_IP
log "Done."
cat <<MSG

  Hosty is running.

  Panel:     http://${PANEL_DOMAIN:-$IP:8800}  (HTTPS via Caddy once HOSTY_PANEL_DOMAIN
             is set and the domain points at this server)
  First run: open the panel and create the admin account (first-boot setup).
  Update:    sudo bash $APP_DIR/installer/update.sh
  Uninstall: sudo bash $APP_DIR/installer/uninstall.sh

MSG
