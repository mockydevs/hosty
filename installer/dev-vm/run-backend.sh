#!/usr/bin/env bash
# Run the API inside the dev VM (root, like production). Invoked by dev-vm.(ps1|sh).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo $0)" >&2; exit 1; }
REPO="${HOSTY_REPO:-/home/ubuntu/hosty}"
set -a
# shellcheck disable=SC1091
. /var/lib/hosty/dev.env
set +a
cd "$REPO/backend"
export UV_PROJECT_ENVIRONMENT=/var/lib/hosty/venv
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8800
