#!/usr/bin/env bash
# Post-deploy smoke test. Read-only: safe against staging and production.
#   scripts/smoke.sh https://api.example.com https://app.example.com
set -euo pipefail
API="${1:?api base url}"; WEB="${2:?web base url}"
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
code() { curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }
[ "$(code "$API/healthz")" = 200 ] || fail "api liveness"
ready="$(curl -sS --max-time 15 "$API/readyz")"; echo "$ready" | grep -q '"status":"ok"' || fail "api readiness: $ready"
meta="$(curl -sS --max-time 15 "$API/v1/meta")"
echo "$meta" | grep -q '"provider_mode":"live"' || fail "deployed API is not using live providers"
echo "$meta" | grep -q '"auth_mode":"clerk"' || fail "deployed API is not using Clerk auth"
[ "$(code "$API/v1/projects")" = 401 ] || fail "unauthenticated request was not rejected"
[ "$(code -X POST "$API/v1/dev/login" -H 'content-type: application/json' -d '{"email":"a@b.co"}')" = 404 ] || fail "dev login is reachable"
[ "$(code -X POST "$API/webhooks/stripe" -d '{}')" = 403 ] || fail "unsigned webhook was not rejected"
[ "$(code "$WEB/sign-in")" = 200 ] || fail "web sign-in page"
curl -sSI --max-time 15 "$WEB/sign-in" | grep -qi '^content-security-policy:' || fail "web CSP header missing"
echo "smoke ok: $API $WEB"
