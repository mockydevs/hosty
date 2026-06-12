#!/usr/bin/env bash
# CI guard (Week 22): structural security invariants, enforced on every build.
# Run from backend/ (CI) or pass the backend dir as $1.
set -euo pipefail
cd "${1:-.}"
[[ -d app ]] || { echo "Run from backend/ (or pass it as \$1)" >&2; exit 2; }

fail=0
check() { # check <description> <grep-args...>
  local desc=$1; shift
  if grep -rn "$@" >/dev/null 2>&1; then
    echo "FAIL: $desc"; grep -rn "$@" | head -5; fail=1
  else
    echo "OK:   $desc"
  fi
}

# 1. No shell interpretation anywhere — argv lists only (ADR-005).
check "no shell=True" "shell=True" app/

# 2. Subprocess use is confined to the single runner module.
if grep -rln "subprocess" app/ --include="*.py" | grep -v "app/system/runner.py"; then
  echo "FAIL: subprocess outside app/system/runner.py"; fail=1
else
  echo "OK:   subprocess only in app/system/runner.py"
fi

# 3. SQL strings are built only in the validated mariadb service (ADR-007).
if grep -rln --include="*.py" -E "(CREATE|DROP) (DATABASE|USER)|GRANT ALL" app/ | grep -v "app/services/mariadb.py"; then
  echo "FAIL: SQL statement strings outside app/services/mariadb.py"; fail=1
else
  echo "OK:   SQL built only in app/services/mariadb.py"
fi

# 4. WP-CLI argv has exactly two owners during the v1 -> v2 transition:
# legacy sites use runuser; the v2 blueprint uses the rootless Podman seam.
if grep -rln --include="*.py" '"wp",' app/ \
  | grep -v -E "app/(services/wordpress|orchestration/blueprints/wordpress)\.py"; then
  echo "FAIL: wp argv built outside the approved WordPress modules"; fail=1
else
  echo "OK:   wp argv only in approved WordPress modules"
fi
if ! grep -q '"runuser"' app/services/wordpress.py; then
  echo "FAIL: wordpress.py no longer runs wp via runuser"; fail=1
else
  echo "OK:   legacy WP-CLI runs via runuser (never root)"
fi
if ! grep -q "podman.run_transient" app/orchestration/blueprints/wordpress.py; then
  echo "FAIL: v2 WordPress no longer runs WP-CLI through rootless Podman"; fail=1
else
  echo "OK:   v2 WP-CLI runs through rootless Podman"
fi

# 5. Request bodies must never be stored in the audit log.
if grep -n "request.body\|await request.json" app/core/middleware.py; then
  echo "FAIL: audit middleware reads request bodies"; fail=1
else
  echo "OK:   audit middleware never reads bodies"
fi

exit $fail
