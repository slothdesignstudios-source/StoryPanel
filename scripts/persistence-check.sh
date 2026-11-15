#!/usr/bin/env bash
# Proves that stored images and paused workflows survive a FULL stop and start of the local stack,
# using a completely separate copy of it: its own database (storypanel_persist_check), bucket
# (storypanel-persist-check), Temporal server and namespace (persist-check), Redis index 7, ports
# (S3 9050, Temporal 7250, API 8050) and data directory. Your comics, images and workflows are not
# touched. Simulated providers only: no paid call.
#
#   scripts/persistence-check.sh          seed a comic, full stop, full start, verify hashes, image
#                                         retrieval and the workflow, approve, stop
#   scripts/persistence-check.sh --live   later, with your keys in .env: start the same isolated copy
#                                         with live providers and verify again. It creates and approves
#                                         nothing, so it makes no paid call.
# Work directory: $SP_PERSIST_WORK (default ~/.storypanel-persistence-check). Remove it, and drop the
# database storypanel_persist_check, to run the check again from scratch.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
W="${SP_PERSIST_WORK:-$HOME/.storypanel-persistence-check}"
export SP_RUN="$W/run" SP_DATA="$W/data" SP_S3_PORT=9050 SP_TEMPORAL_PORT=7250 SP_API_PORT=8050 \
  SP_WEB_PORT=3050 SP_WORKER_METRICS_PORT=9480 SP_SKIP_WEB=1 SP_AUTO_MIGRATE=1 \
  SP_DATABASE_URL="postgresql+asyncpg://storypanel@127.0.0.1:5432/storypanel_persist_check" \
  SP_REDIS_URL="redis://127.0.0.1:6379/7" SP_S3_BUCKET=storypanel-persist-check SP_TEMPORAL_NAMESPACE=persist-check
. "$ROOT/scripts/stack-env.sh"   # storage endpoint and local credentials for the helper (never printed)
STACK="$ROOT/scripts/native-stack.sh"
PY="$ROOT/services/backend/.venv/bin/python"
TEMPORAL_BIN="${TEMPORAL_BIN:-$(command -v temporal || ls /tmp/temporal-sdk-python-* 2>/dev/null | head -1)}"
die() { echo "persistence-check: FAIL $*" >&2; exit 1; }
stack() { "$STACK" "$@"; }
all_stopped() { for n in s3 temporal api worker; do [ -f "$SP_RUN/$n.pid" ] && kill -0 "$(cat "$SP_RUN/$n.pid")" 2>/dev/null && return 1; done; return 0; }
workflow_running() {
  local pid wid
  pid=$(python3 -c "import json;print(json.load(open('$W/state.json'))['project_id'])")
  wid=$(psql -h 127.0.0.1 -U storypanel -d storypanel_persist_check -Atc "select workflow_id from projects where id = '$pid'")
  [ -n "$wid" ] || return 1
  "$TEMPORAL_BIN" workflow describe --workflow-id "$wid" --namespace persist-check --address "127.0.0.1:7250" -o json 2>/dev/null \
    | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin)['workflowExecutionInfo']['status']=='WORKFLOW_EXECUTION_STATUS_RUNNING' else 1)"
}
full_restart() {
  # moto (in-memory) is only ever used here to demonstrate the failure; versity needs no flag
  SP_ALLOW_VOLATILE_STORAGE="${SP_ALLOW_VOLATILE_STORAGE:-0}" stack stop
  all_stopped || die "a process of the isolated stack is still running after stop"
  echo "full stop done: storage, Temporal, API and worker all exited"
  stack start >/dev/null
}

for p in 9050 7250 8050 9480; do
  (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && [ ! -f "$SP_RUN/api.pid" ] && die "port $p is already in use"
done
case "${1:-}" in
  "")
    [ -e "$W/state.json" ] && die "a previous check exists in $W (remove it and drop storypanel_persist_check to start over)"
    mkdir -p "$W"
    psql -h 127.0.0.1 -U storypanel -d postgres -Atc "select 1 from pg_database where datname='storypanel_persist_check'" | grep -q 1 \
      && die "database storypanel_persist_check already exists (drop it first; it is used only by this check)"
    psql -h 127.0.0.1 -U storypanel -d postgres -q -c "create database storypanel_persist_check encoding 'UTF8' template template0"
    psql -h 127.0.0.1 -U storypanel -d storypanel_persist_check -q -c "create extension if not exists vector"
    redis-cli -n 7 flushdb >/dev/null
    echo "== isolated stack (simulated providers), storage: ${SP_LOCAL_S3:-versity}"
    stack start >/dev/null
    "$PY" "$ROOT/scripts/persistence_check.py" seed "$W/state.json"
    full_restart
    workflow_running && echo "PASS workflow still running after the restart (Temporal, namespace persist-check)" \
      || { stack stop >/dev/null 2>&1 || true; die "workflow not running after the restart"; }
    "$PY" "$ROOT/scripts/persistence_check.py" verify "$W/state.json" || { SP_ALLOW_VOLATILE_STORAGE=1 stack stop >/dev/null 2>&1 || true; die "storage did not survive the restart"; }
    "$PY" "$ROOT/scripts/persistence_check.py" approve "$W/state.json"
    stack stop >/dev/null
    touch "$W/simulated.passed"
    echo "persistence-check: PASS (simulated providers). Next, with your keys: scripts/persistence-check.sh --live" ;;
  --live)
    [ -f "$W/simulated.passed" ] || die "run the simulated check first"
    echo "== same isolated stack with LIVE providers (nothing is created or approved: no paid call)"
    SP_STACK_LIVE=1 stack start >/dev/null
    curl -fsS "http://127.0.0.1:8050/v1/meta" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('provider_mode')=='live' else 1)" \
      || { stack stop >/dev/null; die "the isolated API is not in live mode"; }
    echo "PASS isolated API runs live providers"
    workflow_running && echo "PASS workflow still running" || { stack stop >/dev/null; die "workflow not running"; }
    "$PY" "$ROOT/scripts/persistence_check.py" verify "$W/state.json" awaiting_script_approval || { stack stop >/dev/null; die "storage check failed in live mode"; }
    stack stop >/dev/null
    echo "persistence-check: PASS (live mode, isolated, no paid call)" ;;
  *) die "usage: $0 [--live]" ;;
esac
