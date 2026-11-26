#!/usr/bin/env bash
# Read-only Operator Preflight Check for AGCore R1.2 Final (NG-01 / NG-03 / NG-05)
# NEVER prints secret values; safe to run on Raspberry Pi before deployment.
set -euo pipefail

AGTOOLS_URL="${AGTOOLS_URL:-http://127.0.0.1:8006}"
AGCORE_URL="${AGCORE_URL:-http://127.0.0.1:8200}"
TRAJ_DB="${TRAJ_DB:-/var/lib/hd2lab-agcore/trajectory.db}"

echo "=== 1. AGTools Health & Deployed Version (Read-Only) ==="
curl -sS -m 5 "${AGTOOLS_URL}/api/v1/health" | head -c 300 || echo "AGTOOLS_HEALTH_UNREACHABLE"
echo ""

echo "=== 2. AGTools Auth Contract Probe (Invalid Token Rejection) ==="
BAD_CODE=$(curl -sS -o /dev/null -w "%{http_code}" -m 5 \
  -H "X-Service-Token: invalid-probe-token-0000" \
  "${AGTOOLS_URL}/api/v1/tools/manifest" || echo "000")
echo "Invalid token HTTP status (expect 401/403): ${BAD_CODE}"

echo "=== 3. AGCore Required Env Presence Check (Names Only, No Values) ==="
for var in AGCORE_ENV AGCORE_AGTOOLS_ENABLED AGCORE_AGTOOLS_TOKEN AGCORE_TRUSTED_PROXY_SECRET AGCORE_CREDENTIAL_SCOPES AGCORE_CREDENTIAL_SECRETS AGCORE_RUNTIME_ENGINE; do
  if [ -n "${!var:-}" ]; then
    echo "  [SET]   ${var}"
  else
    echo "  [UNSET] ${var}"
  fi
done

echo "=== 4. SQLite Trajectory Dedup Preflight (Read-Only Aggregates) ==="
if [ -f "${TRAJ_DB}" ] && [ -f "$(dirname "$0")/check_sqlite_dedup.sql" ]; then
  sqlite3 -readonly "${TRAJ_DB}" < "$(dirname "$0")/check_sqlite_dedup.sql"
else
  echo "  Skipped (DB or check_sqlite_dedup.sql not found at ${TRAJ_DB})"
fi

echo "=== Preflight Complete (Zero mutations performed) ==="
