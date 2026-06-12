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
# /opt/hosty is a deploy checkout: force it to match the remote exactly.
# (A swallowed `pull --ff-only || true` here once made updates silently no-op.)
if git -C "$APP_DIR" rev-parse -q --verify "origin/$REF" >/dev/null 2>&1; then
  git -C "$APP_DIR" reset --hard "origin/$REF"
fi
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

# pnpm version is pinned via "packageManager" in frontend/package.json;
# corepack fetches exactly that version. Build-script approvals live in
# frontend/pnpm-workspace.yaml (allowBuilds).
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
command -v pnpm >/dev/null || corepack enable

(
  cd "$APP_DIR/frontend"
  pnpm install --frozen-lockfile
  pnpm exec vite build
)

# ── Restart service ──────────────────────────────────────────────────────────
log "Restarting hosty service"
install -m 0644 "$APP_DIR/installer/systemd/hosty.service" /etc/systemd/system/hosty.service
systemctl daemon-reload
systemctl restart hosty
sleep 2
systemctl --no-pager --lines=5 status hosty || true

log "Update complete"
echo "    Running: $(git -C "$APP_DIR" describe --tags --always)"
