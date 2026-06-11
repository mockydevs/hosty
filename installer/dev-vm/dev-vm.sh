#!/usr/bin/env bash
# Hosty dev VM driver for macOS/Linux hosts (Windows: dev-vm.ps1).
# Usage: installer/dev-vm/dev-vm.sh {up|provision|backend|test|smoke|shell|ip|status|down|destroy}
set -euo pipefail

VM=hosty-dev
CPUS=2 MEM=4G DISK=20G
REPO_DIR=$(cd "$(dirname "$0")/../.." && pwd)
MOUNT=/home/ubuntu/hosty

command -v multipass >/dev/null \
  || { echo "Multipass not found — install from https://canonical.com/multipass" >&2; exit 1; }

vexec() { multipass exec "$VM" -- sudo bash -lc "$*"; }
vip() { multipass info "$VM" | awk '/IPv4/{print $2; exit}'; }

case "${1:-up}" in
  up)
    if ! multipass info "$VM" >/dev/null 2>&1; then
      multipass launch 24.04 --name "$VM" --cpus "$CPUS" --memory "$MEM" --disk "$DISK" \
        --cloud-init "$REPO_DIR/installer/dev-vm/cloud-init.yaml"
    else
      multipass start "$VM"
    fi
    if ! multipass info "$VM" | grep -q "$MOUNT"; then
      multipass mount "$REPO_DIR" "$VM:$MOUNT"
    fi
    vexec "bash $MOUNT/installer/provision.sh"
    vexec "bash $MOUNT/installer/dev-vm/vm-setup.sh"
    printf '\nVM ready. IP: %s\n' "$(vip)"
    printf 'Next: %s backend   →  http://%s:8800/api/docs\n' "$0" "$(vip)"
    ;;
  provision)
    vexec "bash $MOUNT/installer/provision.sh && bash $MOUNT/installer/dev-vm/vm-setup.sh"
    ;;
  backend)
    echo "API → http://$(vip):8800  (Ctrl+C stops it)"
    vexec "set -a; . /var/lib/hosty/dev.env; set +a; cd $MOUNT/backend \
      && UV_PROJECT_ENVIRONMENT=/var/lib/hosty/venv exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8800"
    ;;
  test)
    vexec "set -a; . /var/lib/hosty/dev.env; set +a; cd $MOUNT/backend \
      && UV_PROJECT_ENVIRONMENT=/var/lib/hosty/venv uv run pytest -m vm"
    ;;
  smoke) vexec "bash $MOUNT/installer/dev-vm/smoke.sh" ;;
  shell) multipass shell "$VM" ;;
  ip) vip ;;
  status) multipass info "$VM" ;;
  down) multipass stop "$VM" ;;
  destroy) multipass delete --purge "$VM" ;;
  *)
    echo "Usage: $0 {up|provision|backend|test|smoke|shell|ip|status|down|destroy}" >&2
    exit 1
    ;;
esac
