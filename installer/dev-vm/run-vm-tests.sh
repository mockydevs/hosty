#!/usr/bin/env bash
# Run the VM-bound integration tests (pytest -m vm). Invoked by dev-vm.(ps1|sh).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo $0)" >&2; exit 1; }
REPO="${HOSTY_REPO:-/home/ubuntu/hosty}"
set -a
# shellcheck disable=SC1091
. /var/lib/hosty/dev.env
set +a
cd "$REPO/backend"
UV_PROJECT_ENVIRONMENT=/var/lib/hosty/venv uv run pytest -m vm "$@"
