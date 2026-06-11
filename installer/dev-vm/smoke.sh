#!/usr/bin/env bash
# Week 2 smoke test: create a Caddy vhost serving a PHP file and curl it.
#
# Pushes a minimal config through the Caddy admin API — the same mechanism
# the panel uses (ADR-001). Served on :8081 so it never needs certificates.
# NOTE: POST /load replaces the running Caddy config; the panel re-syncs its
# desired state on the next operation, so this is safe on a dev VM only.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo $0)" >&2; exit 1; }

DOCROOT=/var/www/smoke.test
ADMIN=http://127.0.0.1:2019

install -d -m 0755 "$DOCROOT"
cat > "$DOCROOT/index.php" <<'PHP'
<?php echo "hosty-smoke-ok php" . PHP_VERSION;
PHP
chown -R www-data:www-data "$DOCROOT"

CADDYFILE=$(mktemp)
trap 'rm -f "$CADDYFILE"' EXIT
cat > "$CADDYFILE" <<'CF'
{
	admin 127.0.0.1:2019
}
http://:8081 {
	root * /var/www/smoke.test
	php_fastcgi unix//run/php/php8.3-fpm.sock
}
CF

caddy adapt --config "$CADDYFILE" --adapter caddyfile \
  | curl -fsS -X POST "$ADMIN/load" -H "Content-Type: application/json" -d @-

sleep 1
BODY=$(curl -fsS http://127.0.0.1:8081/)
echo "Response: $BODY"
if [[ $BODY == hosty-smoke-ok* ]]; then
  echo "SMOKE TEST PASSED"
else
  echo "SMOKE TEST FAILED" >&2
  exit 1
fi
