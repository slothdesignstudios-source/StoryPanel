#!/usr/bin/env bash
# Build the web app with canary values in every server-side secret variable, then prove
# none of them (and nothing secret-shaped) ends up in what is served to browsers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT/apps/web"
export SP_ANTHROPIC_API_KEY="sk-ant-CANARYbundle0123456789abcdef" SP_FAL_KEY="CANARY-fal-bundle-1a2b3c4d" \
  SP_OPENAI_API_KEY="sk-proj-CANARYopenaiBundle0123456789" SP_GEMINI_API_KEY="AIzaCANARYgeminiBundle0123456789abcdef" \
  SP_STRIPE_SECRET_KEY="sk_live_CANARYbundle0123456789" SP_STRIPE_WEBHOOK_SECRET="whsec_CANARYbundle012345" \
  SP_CLERK_WEBHOOK_SECRET="whsec_CANARYclerk0123456" CLERK_SECRET_KEY="sk_test_CANARYclerkSecret0123" \
  SP_VOYAGE_API_KEY="pa-CANARY-voyage-99887766" SP_DEV_AUTH_SECRET="CANARY-devauth-bundle-5544332211" \
  SP_S3_SECRET_ACCESS_KEY="CANARY-s3-bundle-aabbccdd" SP_TEMPORAL_API_KEY="CANARY-temporal-bundle-eeff0011" \
  NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://localhost:8000}" NEXT_TELEMETRY_DISABLED=1
rm -rf .next && pnpm build >/dev/null 2>&1
fail=0
if grep -rIl "CANARY" .next/static .next/server 2>/dev/null | grep -v '\.map$' | head -5 | grep .; then echo "FAIL: a secret canary is in the build output"; fail=1; fi
# secret-shaped strings in what browsers download
if grep -rIoE "(sk_live_|sk_test_|sk-ant-|sk-proj-|AIza[0-9A-Za-z_-]{20}|whsec_|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)[A-Za-z0-9_-]{6,}" .next/static | head -5 | grep .; then echo "FAIL: secret-shaped string in client bundle"; fail=1; fi
# server-only variable names must not be referenced by client code
if grep -rIoE "SP_[A-Z_]*(KEY|SECRET|TOKEN|PASSWORD)" .next/static | head -5 | grep .; then echo "FAIL: server secret variable name in client bundle"; fail=1; fi
echo "client bundle files scanned: $(find .next/static -type f | wc -l); public env inlined: $(grep -rIoh 'NEXT_PUBLIC_[A-Z_]*' .next/static | sort -u | tr '\n' ' ')"
[ "$fail" = 0 ] && echo "bundle secret scan: PASS"
exit $fail
