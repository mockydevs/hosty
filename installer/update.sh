#!/usr/bin/env bash
# Hosty self-update: pull a ref/tag, sync deps, rebuild frontend, migrate, restart.
#   sudo bash /opt/hosty/installer/update.sh [git-ref]   (default: latest main)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

APP_DIR=/opt/hosty
STATE_DIR=/var/lib/hosty
ENV_FILE=$STATE_DIR/hosty.env
VENV=$STATE_DIR/venv
REF="${1:-main}"

log() { printf '\n==> %s\n' "$*"; }

# ── Preflight checks ─────────────────────────────────────────────────────────
[[ -d $APP_DIR/.git ]] || { echo "ERROR: $APP_DIR is not a git repo. Run the installer first." >&2; exit 1; }
[[ -f $ENV_FILE ]]     || { echo "ERROR: $ENV_FILE not found. Run the installer first." >&2; exit 1; }

# Migrate existing env files to include PDNS API key if missing.
if ! grep -q "^HOSTY_PDNS_API_KEY=" "$ENV_FILE"; then
  echo "HOSTY_PDNS_API_KEY=$(cat /etc/hosty/pdns-api-key 2>/dev/null || echo "")" >> "$ENV_FILE"
fi

# ── Pull latest code ─────────────────────────────────────────────────────────
log "Updating source to $REF"
git -C "$APP_DIR" fetch --tags origin
git -C "$APP_DIR" checkout -q "$REF"
git -C "$APP_DIR" pull -q --ff-only origin "$REF" 2>/dev/null || true
echo "    Commit: $(git -C "$APP_DIR" describe --tags --always)"

# ── Backend dependencies + migrations ────────────────────────────────────────
log "Backend dependencies"
(cd "$APP_DIR/backend" && UV_PROJECT_ENVIRONMENT=$VENV uv sync)

log "Database migrations (alembic upgrade head)"
(cd "$APP_DIR/backend" \
  && set -a && . "$ENV_FILE" && set +a \
  && UV_PROJECT_ENVIRONMENT=$VENV uv run alembic upgrade head)

# ── Frontend rebuild ─────────────────────────────────────────────────────────
log "Frontend rebuild"

build_frontend() {
  cd "$APP_DIR/frontend"

  # Ensure pnpm is available.
  command -v pnpm >/dev/null || { corepack enable; }

  # Strategy 1: normal install.
  if pnpm install 2>/dev/null; then
    pnpm exec vite build && return 0
  fi

  log "Retrying frontend with clean node_modules..."
  # Strategy 2: wipe cached state so pnpm re-reads workspace config.
  rm -rf node_modules .pnpm-store
  if pnpm install 2>/dev/null; then
    pnpm exec vite build && return 0
  fi

  log "Retrying with --ignore-scripts + manual rebuild..."
  # Strategy 3: skip scripts, then rebuild the two packages that need native binaries.
  rm -rf node_modules
  pnpm install --ignore-scripts
  pnpm rebuild @biomejs/biome esbuild 2>/dev/null || true
  pnpm exec vite build && return 0

  echo "ERROR: Frontend build failed after all attempts." >&2
  return 1
}
build_frontend

# ── Restart service ──────────────────────────────────────────────────────────
log "Restarting hosty service"
install -m 0644 "$APP_DIR/installer/systemd/hosty.service" /etc/systemd/system/hosty.service
systemctl daemon-reload
systemctl restart hosty
sleep 2
systemctl --no-pager --lines=5 status hosty || true

log "Update complete"
echo "    Running: $(git -C "$APP_DIR" describe --tags --always)"
