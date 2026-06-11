#!/usr/bin/env bash
# Hosty stack provisioning for Ubuntu 24.04 (dev VM and, later, production).
# Idempotent: safe to re-run; every step checks before it changes anything.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo $0)" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
PHP_VERSIONS=(8.2 8.3 8.4)

log() { printf '\n==> %s\n' "$*"; }

log "Base packages"
apt-get update -q
apt-get install -qy --no-install-recommends \
  ca-certificates curl gnupg software-properties-common debian-keyring \
  debian-archive-keyring apt-transport-https unzip less sqlite3

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
systemctl enable --now caddy

log "MariaDB"
apt-get install -qy mariadb-server
apt-get install -qy zstd   # Phase 8: tar --zstd backups
install -d -m 0700 /var/lib/hosty/backups
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
if ! command -v wp >/dev/null; then
  curl -fsSL -o /usr/local/bin/wp \
    https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar
  chmod +x /usr/local/bin/wp
fi
install -d -m 0777 /var/cache/hosty/wp-cli   # shared WP-CLI download cache

log "Adminer"
install -d -m 0755 /var/lib/hosty/adminer
if [[ ! -f /var/lib/hosty/adminer/adminer.php ]]; then
  # NB: don't use github .../latest/download/adminer.php — Adminer v5 renamed
  # its release assets (adminer-<version>.php), so that URL now 404s.
  curl -fsSL -o /var/lib/hosty/adminer/adminer.php https://www.adminer.org/latest.php
fi
chown -R www-data:www-data /var/lib/hosty/adminer

log "Filebrowser"
if ! command -v filebrowser >/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash
fi
install -d -m 0755 /var/lib/hosty
# Default scope is a quarantine dir: proxy auth AUTO-CREATES unknown users
# with the default scope, and the default must never expose other sites.
if [[ ! -f /var/lib/hosty/filebrowser.db ]]; then
  filebrowser -d /var/lib/hosty/filebrowser.db config init \
    --auth.method=proxy --auth.header=X-Hosty-Fb-User \
    --root=/var/www --scope=/.hosty-quarantine --baseurl=/files \
    --address=127.0.0.1 --port=8082 --signup=false
fi
install -d -m 0755 /var/www/.hosty-quarantine
if [[ ! -f /etc/systemd/system/hosty-filebrowser.service ]]; then
  cat > /etc/systemd/system/hosty-filebrowser.service <<'UNIT'
[Unit]
Description=Hosty Filebrowser (internal)
After=network.target

[Service]
ExecStart=/usr/local/bin/filebrowser -d /var/lib/hosty/filebrowser.db
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
fi
# Admin user for the panel's user-management API (header auth; password is
# random and locked — never used). CLI needs the BoltDB lock: stop the daemon.
systemctl stop hosty-filebrowser 2>/dev/null || true
filebrowser -d /var/lib/hosty/filebrowser.db config set --scope=/.hosty-quarantine --baseurl=/files
ADMIN_ADD_OUT=$(filebrowser -d /var/lib/hosty/filebrowser.db users add admin \
  "$(openssl rand -base64 24)" --perm.admin --lockPassword 2>&1) \
  || echo "$ADMIN_ADD_OUT" | grep -qi "already exists" \
  || { echo "$ADMIN_ADD_OUT" >&2; exit 1; }
systemctl enable --now hosty-filebrowser

log "Site directories"
install -d -m 0755 /var/www

log "Done. Versions:"
caddy version
mariadb --version
wp --version --allow-root
for v in "${PHP_VERSIONS[@]}"; do "php-fpm${v}" -v | head -1; done
