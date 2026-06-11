#!/usr/bin/env bash
# Hosty uninstall (Week 25). Removes the panel; by default KEEPS sites,
# databases and backups (they are user data). --purge-all removes everything.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

PURGE=${1:-}

echo "==> Stopping panel"
systemctl disable --now hosty 2>/dev/null || true
rm -f /etc/systemd/system/hosty.service
systemctl daemon-reload

echo "==> Removing panel code and state"
rm -rf /opt/hosty /var/lib/hosty/venv /var/lib/hosty/hosty.db /var/lib/hosty/hosty.env

if [[ $PURGE == "--purge-all" ]]; then
  echo "==> PURGE: sites, site users, databases, backups, stack services"
  systemctl disable --now hosty-filebrowser 2>/dev/null || true
  rm -f /etc/systemd/system/hosty-filebrowser.service
  for user in $(awk -F: '/^site-/{print $1}' /etc/passwd); do
    userdel --remove "$user" 2>/dev/null || true
  done
  rm -rf /var/www/* /var/lib/hosty
  rm -f /etc/php/*/fpm/pool.d/site-*.conf /etc/php/*/fpm/pool.d/hosty-adminer.conf
  echo "NOTE: Caddy/PHP/MariaDB/PowerDNS packages were left installed;"
  echo "      remove them with apt if you want a fully clean machine."
else
  echo "Kept: /var/www (site files), MariaDB databases, /var/lib/hosty/backups."
  echo "Run with --purge-all to remove sites and data too."
fi
echo "Done."
