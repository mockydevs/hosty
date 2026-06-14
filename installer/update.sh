#!/usr/bin/env bash
# Hosty self-update: pull a ref/tag, sync deps, rebuild frontend, migrate, restart.
#   sudo bash /opt/hosty/installer/update.sh [git-ref]   (default: latest main)
set -euo pipefail
cd /  # guard against a deleted/inaccessible caller cwd
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
(cd "$APP_DIR/backend" && UV_PROJECT_ENVIRONMENT=$VENV uv sync --frozen)

log "Database migrations (alembic upgrade head)"

# ── Migration helper ─────────────────────────────────────────────────────────
_alembic() { cd "$APP_DIR/backend" && set -a && . "$ENV_FILE" && set +a \
             && UV_PROJECT_ENVIRONMENT=$VENV uv run alembic "$@"; }

# Capture the DB revision before touching it so we know what to restore on failure.
_pre_rev=$(_alembic current 2>/dev/null | awk '{print $1}' | head -1 || echo "unknown")

# Run migrations with auto-heal: if a migration fails because an object
# (index, table, column) already exists in the DB, stamp that revision as
# applied and retry — up to 5 times.  This handles the common case where
# SQLAlchemy's create_tables_on_startup (or a previous partial run) already
# created the object the migration wants to create.
_heal_attempts=0
_max_heals=5
while true; do
  _out=$(_alembic upgrade head 2>&1) && break   # success → exit loop

  # Check if it's a recoverable "already exists" conflict.
  if echo "$_out" | grep -qiE "already exists|duplicate (column|key|index|table)"; then
    if [ $_heal_attempts -ge $_max_heals ]; then
      echo "ERROR: still failing after $_max_heals auto-heal attempts." >&2
      echo "$_out" >&2
      break   # fall through to the failure block below
    fi
    _heal_attempts=$((_heal_attempts + 1))

    # Extract the target revision from "Running upgrade X -> Y" in the output.
    _fail_rev=$(echo "$_out" | grep -oE "Running upgrade [^ ]+ -> [^ ]+" \
                | tail -1 | awk '{print $NF}')
    if [ -z "$_fail_rev" ]; then
      echo "ERROR: could not identify failing revision; manual fix required." >&2
      echo "$_out" >&2
      break
    fi

    echo "    Auto-heal attempt $_heal_attempts: stamping '$_fail_rev' (object already exists)."
    _alembic stamp "$_fail_rev" 2>/dev/null || true
    continue
  fi

  # Not a recoverable error — print it and fall through to the failure block.
  echo "$_out" >&2
  break
done

# Verify we are actually at head now.
if ! _alembic check >/dev/null 2>&1; then
  echo ""
  echo "┌─────────────────────────────────────────────────────────────┐"
  echo "│  MIGRATION FAILED — service has NOT been restarted          │"
  echo "│  The running instance is still serving the old schema.      │"
  echo "│                                                             │"
  printf  "│  DB was at: %-48s│\n" "$_pre_rev"
  echo "│                                                             │"
  echo "│  To rollback the code to match the DB:                     │"
  printf  "│    cd /opt/hosty && git checkout %-27s│\n" "$_pre_rev"
  echo "│                                                             │"
  echo "│  Fix the migration then re-run update.sh.                  │"
  echo "└─────────────────────────────────────────────────────────────┘"
  exit 1
fi

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
# ── Host setup ───────────────────────────────────────────────────────────────
log "Applying host sysctl prerequisites"
if ! grep -q "net.ipv4.ip_unprivileged_port_start" /etc/sysctl.d/99-hosty-rootless.conf 2>/dev/null; then
  echo "net.ipv4.ip_unprivileged_port_start = 80" >> /etc/sysctl.d/99-hosty-rootless.conf
fi
sysctl -e -p /etc/sysctl.d/99-hosty-rootless.conf >/dev/null 2>&1 || true

# ── Restart service ──────────────────────────────────────────────────────────
log "Restarting hosty service"
install -m 0644 "$APP_DIR/installer/systemd/hosty.service" /etc/systemd/system/hosty.service
systemctl daemon-reload
systemctl restart hosty
sleep 2
systemctl --no-pager --lines=5 status hosty || true

log "Update complete"
echo "    Running: $(git -C "$APP_DIR" describe --tags --always)"
