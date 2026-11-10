#!/usr/bin/env bash
# Runs the whole app without Docker, with fake providers: S3 (moto), Temporal dev server,
# API, worker and web. Needs scripts/native-services.sh (Postgres + Redis) running first.
# Usage: scripts/native-stack.sh start|stop     Logs: $SP_RUN/*.log
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SP_RUN="${SP_RUN:-/tmp/storypanel-run}"
mkdir -p "$SP_RUN"
export SP_ENV=local SP_PROVIDER_MODE=fake SP_AUTH_MODE=dev SP_LOG_LEVEL=INFO
export SP_DATABASE_URL="${SP_DATABASE_URL:-postgresql+asyncpg://storypanel@127.0.0.1:5432/storypanel}"
export SP_REDIS_URL="${SP_REDIS_URL:-redis://127.0.0.1:6379/0}"
export SP_S3_ENDPOINT_URL=http://127.0.0.1:9000 SP_S3_ACCESS_KEY_ID=local SP_S3_SECRET_ACCESS_KEY=local
export SP_TEMPORAL_ADDRESS=127.0.0.1:7233 SP_CORS_ORIGINS='["http://localhost:3000"]'
export SP_SIGNUP_BONUS_CREDITS="${SP_SIGNUP_BONUS_CREDITS:-60}" NO_PROXY=127.0.0.1,localhost
TEMPORAL_BIN="${TEMPORAL_BIN:-$(command -v temporal || ls /tmp/temporal-sdk-python-* 2>/dev/null | head -1)}"
launch() { local name="$1"; shift; nohup "$@" >"$SP_RUN/$name.log" 2>&1 & echo $! >"$SP_RUN/$name.pid"; }
wait_http() { for _ in $(seq 1 90); do curl -fsS -o /dev/null "$1" 2>/dev/null && return 0; sleep 1; done; echo "timeout waiting for $1" >&2; return 1; }
case "${1:-start}" in
  start)
    cd "$ROOT/services/backend"
    launch s3 uv run moto_server -H 127.0.0.1 -p 9000
    [ -n "$TEMPORAL_BIN" ] || { echo "temporal CLI not found; set TEMPORAL_BIN" >&2; exit 1; }
    launch temporal "$TEMPORAL_BIN" server start-dev --ip 127.0.0.1 --port 7233 --headless --log-level warn
    uv run alembic upgrade head >"$SP_RUN/migrate.log" 2>&1
    sleep 3
    launch api uv run uvicorn storypanel.api.app:app_factory --factory --host 127.0.0.1 --port 8000
    SP_METRICS_PORT=9464 launch worker uv run python -m storypanel.workflows.worker
    cd "$ROOT/apps/web"
    [ -d .next/standalone ] || NEXT_TELEMETRY_DISABLED=1 pnpm build >"$SP_RUN/web-build.log" 2>&1
    cp -r .next/static .next/standalone/.next/ 2>/dev/null || true
    PORT=3000 HOSTNAME=127.0.0.1 launch web node .next/standalone/server.js
    wait_http http://127.0.0.1:8000/readyz && wait_http http://127.0.0.1:3000/sign-in
    echo "web http://localhost:3000  api http://127.0.0.1:8000  logs $SP_RUN" ;;
  stop)
    for f in "$SP_RUN"/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done ;;
esac
