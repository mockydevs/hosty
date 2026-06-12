#!/usr/bin/env bash
# Hosty stack provisioning for Ubuntu 24.04 (dev VM and, later, production).
# Idempotent: safe to re-run; every step checks before it changes anything.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo $0)" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
PHP_VERSIONS=(8.2 8.3 8.4)

log() { printf '\n==> %s\n' "$*"; }

# Hosty's web server (Caddy) must own ports 80 and 443. If another web stack
# holds them (Traefik/nginx/Apache from a previous Coolify/Docker/LAMP setup),
# Caddy cannot bind: every site silently answers through the FOREIGN proxy
# (e.g. Traefik's "no available server") and HTTPS certificates never issue.
# Refuse early with a clear message instead.
if [[ -z "${HOSTY_SKIP_PORT_CHECK:-}" ]]; then
  for p in 80 443; do
    holder=$(ss -H -tlnp "sport = :$p" 2>/dev/null | head -1)
    if [[ -n $holder && $holder != *'"caddy"'* ]]; then
      echo "ERROR: port $p is already in use:" >&2
      echo "  $holder" >&2
      echo "Stop and disable/remove the conflicting web stack first (for Docker:" >&2
      echo "  docker ps   to find it, then stop the proxy container/compose stack)," >&2
      echo "then re-run. To override anyway: HOSTY_SKIP_PORT_CHECK=1" >&2
      exit 1
    fi
  done
fi

log "Base packages"
apt-get update -q
apt-get install -qy --no-install-recommends \
  acl ca-certificates curl gnupg software-properties-common debian-keyring \
  debian-archive-keyring apt-transport-https unzip less sqlite3 xz-utils

log "PHP ${PHP_VERSIONS[*]} via ondrej/php PPA"
if ! grep -rq "ondrej/php" /etc/apt/sources.list.d/ 2>/dev/null; then
  add-apt-repository -y ppa:ondrej/php
fi
PHP_PACKAGES=()
for v in "${PHP_VERSIONS[@]}"; do
  PHP_PACKAGES+=(
    "php${v}-fpm" "php${v}-cli" "php${v}-mysql" "php${v}-curl" "php${v}-gd"
    "php${v}-mbstring" "php${v}-xml" "php${v}-zip" "php${v}-intl" "php${v}-imagick"
  )
done
apt-get install -qy "${PHP_PACKAGES[@]}"
for v in "${PHP_VERSIONS[@]}"; do
  systemctl enable --now "php${v}-fpm"
done
install -d -m 0755 /run/php

log "Caddy (official repo)"
if [[ ! -f /etc/apt/sources.list.d/caddy-stable.list ]]; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
fi
apt-get install -qy caddy
# The stock Caddyfile serves a "Congratulations" page on :80 for EVERY domain
# and resurrects on each caddy restart, masking panel-managed sites until the
# panel's next sync. Replace it once with an empty config: the panel publishes
# the real config through the admin API (and re-publishes at panel startup).
if ! grep -q "Managed by Hosty" /etc/caddy/Caddyfile 2>/dev/null; then
  cat > /etc/caddy/Caddyfile <<'CADDYFILE'
# Managed by Hosty — do not edit. Site configuration is published at runtime
# through the Caddy admin API by the Hosty panel.
CADDYFILE
fi
systemctl enable --now caddy

log "MariaDB"
apt-get install -qy mariadb-server
apt-get install -qy zstd   # Phase 8: tar --zstd backups
install -d -m 0700 /var/lib/hosty/backups
id -u hosty-restore >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hosty-restore
install -d -o root -g root -m 0711 /var/lib/hosty/restore-staging
systemctl enable --now mariadb

log "PowerDNS"
# An authoritative DNS server needs port 53's TCP wildcard bind, which
# systemd-resolved's stub listener (127.0.0.53:53) blocks. Disable the stub
# and point resolv.conf at resolved's upstream file so the system keeps DNS.
if [[ ! -f /etc/systemd/resolved.conf.d/hosty-no-stub.conf ]]; then
  install -d -m 0755 /etc/systemd/resolved.conf.d
  printf '[Resolve]\nDNSStubListener=no\n' > /etc/systemd/resolved.conf.d/hosty-no-stub.conf
  ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
  systemctl restart systemd-resolved
fi
apt-get install -qy pdns-server pdns-backend-sqlite3
# The package ships no usable backend (pdns refuses to start without one).
# Configure gsqlite3 — Phase 7 manages zones through it.
if [[ ! -f /var/lib/powerdns/pdns.sqlite3 ]]; then
  install -d -m 0755 /var/lib/powerdns
  sqlite3 /var/lib/powerdns/pdns.sqlite3 \
    < /usr/share/pdns-backend-sqlite3/schema/schema.sqlite3.sql
  chown pdns:pdns /var/lib/powerdns/pdns.sqlite3
fi
rm -f /etc/powerdns/pdns.d/bind.conf   # drop the default bind backend
if [[ ! -f /etc/powerdns/pdns.d/hosty.conf ]]; then
  PDNS_API_KEY=$(openssl rand -hex 16)
  install -d -m 0750 /etc/hosty
  printf '%s' "$PDNS_API_KEY" > /etc/hosty/pdns-api-key   # for HOSTY_PDNS_API_KEY
  chmod 640 /etc/hosty/pdns-api-key
  cat > /etc/powerdns/pdns.d/hosty.conf <<CONF
launch=gsqlite3
gsqlite3-database=/var/lib/powerdns/pdns.sqlite3
# REST API on localhost only (Phase 7: panel manages zones through it)
api=yes
api-key=${PDNS_API_KEY}
webserver=yes
webserver-address=127.0.0.1
webserver-port=8083
CONF
  chmod 640 /etc/powerdns/pdns.d/hosty.conf
  chown root:pdns /etc/powerdns/pdns.d/hosty.conf
fi
systemctl enable pdns
systemctl restart pdns

log "WP-CLI"
bash "$SCRIPT_DIR/install-tools.sh" wp-cli
install -d -m 1777 /var/cache/hosty/wp-cli

log "Adminer"
bash "$SCRIPT_DIR/install-tools.sh" adminer
chown -R www-data:www-data /var/lib/hosty/adminer

log "Filebrowser"
bash "$SCRIPT_DIR/install-tools.sh" filebrowser
id -u hosty-filebrowser >/dev/null 2>&1 || useradd --system --home-dir /nonexistent \
  --shell /usr/sbin/nologin --user-group hosty-filebrowser
install -d -o hosty-filebrowser -g hosty-filebrowser -m 0700 /var/lib/hosty/filebrowser
if [[ -f /var/lib/hosty/filebrowser.db && ! -f /var/lib/hosty/filebrowser/filebrowser.db ]]; then
  mv /var/lib/hosty/filebrowser.db /var/lib/hosty/filebrowser/filebrowser.db
fi
FILEBROWSER_DB=/var/lib/hosty/filebrowser/filebrowser.db
# Default scope is a quarantine dir: proxy auth AUTO-CREATES unknown users
# with the default scope, and the default must never expose other sites.
if [[ ! -f $FILEBROWSER_DB ]]; then
  filebrowser -d "$FILEBROWSER_DB" config init \
    --auth.method=proxy --auth.header=X-Hosty-Fb-User \
    --root=/var/www --scope=/.hosty-quarantine --baseurl=/files \
    --branding.theme=dark \
    --address=127.0.0.1 --port=8082 --signup=false
fi
install -d -m 0755 /var/www/.hosty-quarantine
chown hosty-filebrowser:hosty-filebrowser /var/www/.hosty-quarantine
cat > /etc/systemd/system/hosty-filebrowser.service <<'UNIT'
[Unit]
Description=Hosty Filebrowser (internal)
After=network.target

[Service]
User=hosty-filebrowser
Group=hosty-filebrowser
ExecStart=/usr/local/bin/filebrowser -d /var/lib/hosty/filebrowser/filebrowser.db
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/hosty/filebrowser /var/www

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
# Admin user for the panel's user-management API (header auth; password is
# random and locked — never used). CLI needs the BoltDB lock: stop the daemon.
systemctl stop hosty-filebrowser 2>/dev/null || true
# --branding.theme=dark: embedded in the (dark) panel UI — match it.
filebrowser -d "$FILEBROWSER_DB" config set \
  --scope=/.hosty-quarantine --baseurl=/files --branding.theme=dark
ADMIN_ADD_OUT=$(filebrowser -d "$FILEBROWSER_DB" users add admin \
  "$(openssl rand -base64 24)" --perm.admin --lockPassword 2>&1) \
  || echo "$ADMIN_ADD_OUT" | grep -qi "already exists" \
  || { echo "$ADMIN_ADD_OUT" >&2; exit 1; }
chown -R hosty-filebrowser:hosty-filebrowser /var/lib/hosty/filebrowser
systemctl enable --now hosty-filebrowser

log "Site directories"
install -d -o root -g root -m 0711 /var/www
# Upgrade existing sites to the same tenant boundary used for new sites.
for doc_root in /var/www/*/public_html; do
  [[ -d $doc_root && ! -L $doc_root ]] || continue
  site_dir=${doc_root%/public_html}
  [[ ! -L $site_dir ]] || { echo "Refusing symlinked site directory: $site_dir" >&2; exit 1; }
  site_user=$(stat -c '%U' "$doc_root")
  [[ $site_user =~ ^site-[a-z0-9-]+-[a-f0-9]{6}$ ]] || {
    echo "Refusing unexpected site owner $site_user for $doc_root" >&2
    exit 1
  }
  chown "root:$site_user" "$site_dir"
  chmod 0710 "$site_dir"
  find "$doc_root" -type d -exec chmod 0750 {} +
  find "$doc_root" -type f -exec chmod 0640 {} +
  setfacl -m u:caddy:--x,u:hosty-filebrowser:--x "$site_dir"
  setfacl -R -m u:caddy:r-X,u:hosty-filebrowser:rwx,o::--- "$doc_root"
  setfacl -m d:u:caddy:r-X,d:u:hosty-filebrowser:rwx,d:o::--- "$doc_root"
done

log "Done. Versions:"
caddy version
mariadb --version
wp --version --allow-root
for v in "${PHP_VERSIONS[@]}"; do "php-fpm${v}" -v | head -1; done
