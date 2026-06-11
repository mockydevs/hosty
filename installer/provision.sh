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
  debian-archive-keyring apt-transport-https unzip less

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
systemctl enable --now mariadb

log "PowerDNS"
apt-get install -qy pdns-server pdns-backend-sqlite3
systemctl enable --now pdns

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
  curl -fsSL -o /var/lib/hosty/adminer/adminer.php \
    https://github.com/vrana/adminer/releases/latest/download/adminer.php
fi
chown -R www-data:www-data /var/lib/hosty/adminer

log "Filebrowser"
if ! command -v filebrowser >/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash
fi
install -d -m 0755 /var/lib/hosty
if [[ ! -f /var/lib/hosty/filebrowser.db ]]; then
  filebrowser -d /var/lib/hosty/filebrowser.db config init \
    --auth.method=proxy --auth.header=X-Hosty-Fb-User \
    --root=/var/www --address=127.0.0.1 --port=8082 --signup=false
fi
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
systemctl enable --now hosty-filebrowser

log "Site directories"
install -d -m 0755 /var/www

log "Done. Versions:"
caddy version
mariadb --version
wp --version --allow-root
for v in "${PHP_VERSIONS[@]}"; do "php-fpm${v}" -v | head -1; done
