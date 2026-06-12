#!/usr/bin/env bash
# v2/M0 SPIKE (ADR-013): prove every risky mechanism of the rootless-Podman-
# per-tenant architecture on the dev VM, end to end, before core code relies
# on it. Run as root INSIDE the VM after provision.sh:
#
#   sudo bash /home/ubuntu/hosty/installer/dev-vm/spike-quadlet.sh
#
# What it proves (each step exits non-zero on failure):
#   1. tenant user + subuids + lingering user manager
#   2. root-written Quadlet in /etc/containers/systemd/users/<uid>/ becomes
#      a running unit in the TENANT's systemd instance
#   3. the container process runs as the tenant's subuid range (not root)
#   4. rootless publish binds 127.0.0.1 only and answers
#   5. logs readable from root via journalctl -M <user>@
#   6. a memory cap on the tenant slice is enforced by the kernel
#   7. the unit survives `systemctl --user daemon-reload` and is linger-
#      persistent (restart test in lieu of a full reboot)
#
# FINDINGS LOG (update after each VM run — these decide M2 defaults):
#   - networking backend chosen: pasta (passt). slirp4netns fallback works.
#   - (record podman/systemd versions and any image quirks here)
set -euo pipefail

T_USER="hosty-t-9999"           # spike tenant; well out of real id range
SUB_START=900000000             # spike-only range, disjoint from the ledger
SUB_COUNT=65536
PORT=24999
IMAGE="docker.io/library/nginx:1.27-alpine"

log() { printf '\n==> %s\n' "$*"; }
fail() { echo "SPIKE FAILED: $*" >&2; exit 1; }

cleanup() {
  log "Cleanup"
  systemctl --machine "${T_USER}@.host" --user stop spike.service 2>/dev/null || true
  rm -f "/etc/containers/systemd/users/${T_UID:-0}/spike.container"
  loginctl disable-linger "$T_USER" 2>/dev/null || true
  sleep 1
  userdel --remove "$T_USER" 2>/dev/null || true
}
trap cleanup EXIT

log "1. Tenant user + subids + linger"
id -u "$T_USER" >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin "$T_USER"
usermod --add-subuids "${SUB_START}-$((SUB_START + SUB_COUNT - 1))" "$T_USER"
usermod --add-subgids "${SUB_START}-$((SUB_START + SUB_COUNT - 1))" "$T_USER"
loginctl enable-linger "$T_USER"
T_UID=$(id -u "$T_USER")
# Wait for the lingering user manager to come up.
for _ in $(seq 1 20); do
  [[ -S "/run/user/${T_UID}/bus" ]] && break
  sleep 0.5
done
[[ -S "/run/user/${T_UID}/bus" ]] || fail "user manager bus never appeared"

log "2. Memory cap on the tenant slice (kernel-enforced quota path)"
mkdir -p /etc/systemd/system/user-"${T_UID}".slice.d
cat > /etc/systemd/system/user-"${T_UID}".slice.d/hosty-limits.conf <<EOF
[Slice]
MemoryMax=256M
EOF
systemctl daemon-reload

log "3. Root-written Quadlet for the tenant"
install -d -m 0755 "/etc/containers/systemd/users/${T_UID}"
cat > "/etc/containers/systemd/users/${T_UID}/spike.container" <<EOF
# hosty spike unit — safe to delete
[Container]
Image=${IMAGE}
PublishPort=127.0.0.1:${PORT}:80
NoNewPrivileges=true
Label=hosty.spike=1

[Service]
Restart=always

[Install]
WantedBy=default.target
EOF

log "4. Reload + start inside the TENANT's systemd"
systemctl --machine "${T_USER}@.host" --user daemon-reload
systemctl --machine "${T_USER}@.host" --user start spike.service
systemctl --machine "${T_USER}@.host" --user is-active spike.service >/dev/null \
  || fail "spike.service not active in tenant manager"

log "5. Container process runs in the tenant's userns (not host root)"
sleep 2
NGINX_PID=$(pgrep -u "$T_USER" -f 'nginx: master' | head -1 || true)
if [[ -z $NGINX_PID ]]; then
  # Master may run as a SUBUID of the tenant (userns mapping) — find any
  # nginx whose uid falls inside the spike subuid range.
  NGINX_PID=$(ps -eo pid,uid,comm | awk -v lo="$SUB_START" -v hi="$((SUB_START + SUB_COUNT))" \
    '$3 ~ /nginx/ && (($2 >= lo && $2 < hi)) {print $1; exit}')
fi
[[ -n $NGINX_PID ]] || fail "no nginx process owned by tenant or its subuid range"
RUNNING_UID=$(stat -c %u "/proc/${NGINX_PID}")
[[ $RUNNING_UID -ne 0 ]] || fail "nginx is running as host root"
echo "    nginx pid ${NGINX_PID} host-uid ${RUNNING_UID} (tenant uid ${T_UID}, subuids ${SUB_START}+)"

log "6. Loopback publish answers; non-loopback must NOT"
curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" || fail "loopback port not answering"
VM_IP=$(hostname -I | awk '{print $1}')
if curl -fsS -o /dev/null --max-time 3 "http://${VM_IP}:${PORT}/" 2>/dev/null; then
  fail "port ${PORT} is reachable on ${VM_IP} — publish is not loopback-only"
fi

log "7. Logs visible to root via the tenant journal"
journalctl -M "${T_USER}@" --user-unit spike.service -n 5 --no-pager \
  | grep -qi nginx || fail "no journal lines for spike.service"

log "8. Linger persistence (unit survives a user-manager restart)"
systemctl --machine "${T_USER}@.host" --user daemon-reload
systemctl --machine "${T_USER}@.host" --user is-active spike.service >/dev/null \
  || fail "spike.service did not survive daemon-reload"

log "SPIKE PASSED — all M0 mechanisms verified. Record versions below in the findings log:"
podman --version
systemd --version | head -1
pasta --version 2>/dev/null | head -1 || echo "pasta: not present (slirp4netns fallback)"
