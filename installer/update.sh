#!/usr/bin/env bash
# Hosty self-update (Week 25): pull a ref/tag, sync deps, migrate, restart.
#   sudo bash /opt/hosty/installer/update.sh [git-ref]   (default: latest main)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

APP_DIR=/opt/hosty
STATE_DIR=/var/lib/hosty
REF="${1:-main}"

echo "==> Updating to $REF"
git -C "$APP_DIR" fetch --tags origin
git -C "$APP_DIR" checkout -q "$REF"
git -C "$APP_DIR" pull -q --ff-only origin "$REF" 2>/dev/null || true

echo "==> Backend deps + migrations"
(cd "$APP_DIR/backend" && UV_PROJECT_ENVIRONMENT=$STATE_DIR/venv uv sync)
(cd "$APP_DIR/backend" \
  && set -a && . "$STATE_DIR/hosty.env" && set +a \
  && UV_PROJECT_ENVIRONMENT=$STATE_DIR/venv uv run alembic upgrade head)

echo "==> Frontend rebuild"
(cd "$APP_DIR/frontend" && pnpm install --frozen-lockfile && pnpm exec vite build)

echo "==> Restart"
systemctl restart hosty
systemctl --no-pager --lines=3 status hosty
echo "Updated to: $(git -C "$APP_DIR" describe --tags --always)"
