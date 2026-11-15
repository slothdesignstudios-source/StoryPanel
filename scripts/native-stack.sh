#!/usr/bin/env bash
# Runs the app without Docker: local S3 storage, Temporal dev server, API, worker and web (fake providers;
# SP_STACK_LIVE=1 for live ones). Needs scripts/native-services.sh (Postgres + Redis) running first.
#
#   scripts/native-stack.sh start       storage + Temporal (if not running), then API, worker, web
#   scripts/native-stack.sh stop        API, worker, web, then Temporal, then storage
#   scripts/native-stack.sh start-app   API, worker, web only (storage and Temporal must be running)
#   scripts/native-stack.sh stop-app    API, worker, web only (storage and Temporal keep their state)
#   scripts/native-stack.sh status
#
# Storage (SP_LOCAL_S3): "versity" (default) keeps every object as a file under $SP_DATA/s3, so images
# survive restarts; install it with scripts/install-versitygw.sh. "moto" keeps objects IN MEMORY and
# loses them when it stops: allowed only for simulated runs with SP_ALLOW_VOLATILE_STORAGE=1, and `stop`
# refuses to stop it while it holds objects unless that flag is set.
# Temporal keeps its state in $SP_DATA/temporal.db. `stop` refuses to stop a Temporal started IN MEMORY
# (by an older version of this script) unless SP_ALLOW_TEMPORAL_RESET=1, because that deletes every workflow. The app refuses to start on a database that is not
# at the schema this code expects (SP_AUTO_MIGRATE=1 migrates; use it only for throwaway databases).
# Ports and names: SP_S3_PORT, SP_TEMPORAL_PORT, SP_API_PORT, SP_WEB_PORT, SP_WORKER_METRICS_PORT,
# SP_TEMPORAL_NAMESPACE, SP_S3_BUCKET, SP_DATABASE_URL, SP_RUN, SP_DATA; SP_SKIP_WEB=1 skips the web app.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SP_RUN="${SP_RUN:-/tmp/storypanel-run}"
SP_DATA="${SP_DATA:-/tmp/storypanel-data}"  # same place as scripts/native-services.sh keeps Postgres
mkdir -p "$SP_RUN"
. "$ROOT/scripts/stack-env.sh"   # SP_STACK_LIVE=1: live providers from config/live-smoke.env + .env
TEMPORAL_BIN="${TEMPORAL_BIN:-$(command -v temporal || ls /tmp/temporal-sdk-python-* 2>/dev/null | head -1)}"
VERSITYGW_BIN="${VERSITYGW_BIN:-$(command -v versitygw || ls "$HOME/.local/bin/versitygw" "$HOME/go/bin/versitygw" 2>/dev/null | head -1 || true)}"

launch() { local name="$1"; shift; nohup "$@" >"$SP_RUN/$name.log" 2>&1 & echo $! >"$SP_RUN/$name.pid"; }
alive() { local f="$SP_RUN/$1.pid"; [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; }
die() { echo "native-stack: $*" >&2; exit 1; }
wait_http() { for _ in $(seq 1 90); do curl -fsS -o /dev/null "$1" 2>/dev/null && return 0; sleep 1; done; die "timeout waiting for $1"; }
wait_port() { for _ in $(seq 1 60); do (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0; sleep 1; done; die "nothing listening on port $1"; }
stop_proc() {  # stop one process started by launch and wait until it has exited
  local n="$1" f="$SP_RUN/$1.pid" pid
  [ -f "$f" ] || return 0
  pid=$(cat "$f")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && die "$n (pid $pid) did not stop"
  fi
  rm -f "$f"
}
storage_kind() {  # what is really running: versity | moto | none
  alive s3 || { echo none; return; }
  tr '\0' ' ' <"/proc/$(cat "$SP_RUN/s3.pid")/cmdline" | grep -q moto_server && echo moto || echo versity
}
volatile_objects() {  # objects held by a running in-memory moto (moto accepts unsigned listing); "unknown" on error
  python3 - "$SP_S3_ENDPOINT_URL" "$SP_S3_BUCKET" <<'PY' 2>/dev/null || echo unknown
import sys, urllib.error, urllib.parse, urllib.request, xml.etree.ElementTree as ET
base, bucket, ns, n, token = sys.argv[1].rstrip("/"), sys.argv[2], "{http://s3.amazonaws.com/doc/2006-03-01/}", 0, None
while True:
    q = {"list-type": "2", **({"continuation-token": token} if token else {})}
    try:
        root = ET.fromstring(urllib.request.urlopen(f"{base}/{bucket}?{urllib.parse.urlencode(q)}", timeout=20).read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(0); sys.exit(0)  # no bucket: nothing stored
        raise
    n += sum(1 for _ in root.iter(f"{ns}Contents"))
    if (root.findtext(f"{ns}IsTruncated") or "false") != "true":
        break
    token = root.findtext(f"{ns}NextContinuationToken")
print(n)
PY
}

start_storage() {
  local kind; kind=$(storage_kind)
  if [ "$kind" != none ]; then
    [ "$kind" = "$SP_LOCAL_S3" ] || die "storage running is $kind but SP_LOCAL_S3=$SP_LOCAL_S3; migrate the objects first (docs/local-upgrade-procedure.md)"
    return 0
  fi
  case "$SP_LOCAL_S3" in
    versity)
      [ -n "$VERSITYGW_BIN" ] && [ -x "$VERSITYGW_BIN" ] || die "durable local storage needs versitygw: run scripts/install-versitygw.sh (or set VERSITYGW_BIN)"
      mkdir -p "$SP_DATA/s3"
      # Object metadata lives in extended attributes; where the filesystem has none (e.g. a folder on
      # /mnt/c), it goes into a sidecar directory. --fsync: an acknowledged object is on stable storage.
      local meta=()
      python3 -c "import os,sys; p=sys.argv[1]+'/.xattr-probe'; open(p,'w').close(); os.setxattr(p,'user.probe',b'1'); os.remove(p)" "$SP_DATA/s3" 2>/dev/null \
        || { rm -f "$SP_DATA/s3/.xattr-probe"; mkdir -p "$SP_DATA/s3-meta"; meta=(--sidecar "$SP_DATA/s3-meta"); }
      ROOT_ACCESS_KEY_ID="$SP_S3_ACCESS_KEY_ID" ROOT_SECRET_ACCESS_KEY="$SP_S3_SECRET_ACCESS_KEY" \
        launch s3 "$VERSITYGW_BIN" --port "127.0.0.1:$SP_S3_PORT" posix --fsync "${meta[@]}" "$SP_DATA/s3" ;;
    moto)
      [ "${SP_ALLOW_VOLATILE_STORAGE:-0}" = 1 ] || die "SP_LOCAL_S3=moto loses every image when it stops; set SP_ALLOW_VOLATILE_STORAGE=1 to accept that (simulated runs only)"
      [ "${SP_STACK_LIVE:-0}" != 1 ] || die "refusing in-memory storage with live providers: paid images would be lost on restart"
      (cd "$ROOT/services/backend" && launch s3 uv run moto_server -H 127.0.0.1 -p "$SP_S3_PORT")
      echo "native-stack: WARNING storage is IN MEMORY (moto): stored images are lost when it stops" >&2 ;;
    *) die "SP_LOCAL_S3 must be versity or moto" ;;
  esac
  wait_port "$SP_S3_PORT"
}

start_temporal() {
  alive temporal && return 0
  [ -n "$TEMPORAL_BIN" ] || die "temporal CLI not found; set TEMPORAL_BIN"
  mkdir -p "$SP_DATA"
  # File-backed: workflows paused at an approval survive a restart (in memory they would be lost).
  local ns=(); [ "$SP_TEMPORAL_NAMESPACE" = default ] || ns=(--namespace "$SP_TEMPORAL_NAMESPACE")
  launch temporal "$TEMPORAL_BIN" server start-dev --ip 127.0.0.1 --port "$SP_TEMPORAL_PORT" --headless \
    --log-level warn --db-filename "$SP_DATA/temporal.db" "${ns[@]}"
  wait_port "$SP_TEMPORAL_PORT"
}

check_schema() {
  local cur head
  cur=$(cd "$ROOT/services/backend" && uv run alembic current 2>/dev/null | awk 'NF{print $1}' | tail -1)
  head=$(cd "$ROOT/services/backend" && uv run alembic heads 2>/dev/null | awk 'NF{print $1}' | tail -1)
  [ -n "$head" ] || die "cannot read the expected schema revision"
  [ "$cur" = "$head" ] && return 0
  if [ "${SP_AUTO_MIGRATE:-0}" = 1 ]; then
    (cd "$ROOT/services/backend" && uv run alembic upgrade head >"$SP_RUN/migrate.log" 2>&1) || die "migration failed: $SP_RUN/migrate.log"
  else
    die "database schema is '${cur:-unknown}', this code expects '$head': back up and migrate first (docs/local-upgrade-procedure.md); SP_AUTO_MIGRATE=1 only for throwaway databases"
  fi
}

start_app() {
  [ "$(storage_kind)" != none ] && alive temporal || die "storage and Temporal must be running (use start)"
  [ "$(storage_kind)" = "$SP_LOCAL_S3" ] || die "storage running is $(storage_kind) but SP_LOCAL_S3=$SP_LOCAL_S3"
  check_schema
  cd "$ROOT/services/backend"
  alive api || launch api uv run uvicorn storypanel.api.app:app_factory --factory --host 127.0.0.1 --port "$SP_API_PORT"
  alive worker || SP_METRICS_PORT="${SP_WORKER_METRICS_PORT:-9464}" launch worker uv run python -m storypanel.workflows.worker
  if [ "${SP_SKIP_WEB:-0}" != 1 ] && ! alive web; then
    cd "$ROOT/apps/web"
    [ -d .next/standalone ] || NEXT_TELEMETRY_DISABLED=1 pnpm build >"$SP_RUN/web-build.log" 2>&1
    cp -r .next/static .next/standalone/.next/ 2>/dev/null || true
    cp -r public .next/standalone/ 2>/dev/null || true  # standalone output does not include public/
    PORT="$SP_WEB_PORT" HOSTNAME=127.0.0.1 launch web env -u SP_ANTHROPIC_API_KEY -u SP_OPENAI_API_KEY -u SP_GEMINI_API_KEY -u SP_FAL_KEY -u SP_VOYAGE_API_KEY node .next/standalone/server.js
  fi
  wait_http "http://127.0.0.1:$SP_API_PORT/readyz"
  [ "${SP_SKIP_WEB:-0}" = 1 ] || wait_http "http://127.0.0.1:$SP_WEB_PORT/sign-in"
  echo "web http://localhost:$SP_WEB_PORT  api http://127.0.0.1:$SP_API_PORT  storage $(storage_kind)  logs $SP_RUN"
}

stop_app() { stop_proc web; stop_proc api; stop_proc worker; }

stop_all() {
  stop_app
  if [ "$(storage_kind)" = moto ]; then
    local n; n=$(volatile_objects)
    if [ "$n" != 0 ] && [ "${SP_ALLOW_VOLATILE_STORAGE:-0}" != 1 ]; then
      die "in-memory storage holds ${n} object(s) that stopping would delete. Export them first (scripts/s3-snapshot.py export DIR), then stop with SP_ALLOW_VOLATILE_STORAGE=1. API, worker and web are stopped; storage and Temporal are still running."
    fi
  fi
  if alive temporal && ! tr '\0' ' ' <"/proc/$(cat "$SP_RUN/temporal.pid")/cmdline" | grep -q -- '--db-filename' \
     && [ "${SP_ALLOW_TEMPORAL_RESET:-0}" != 1 ]; then
    die "Temporal is running IN MEMORY: stopping it deletes every running and waiting workflow (an empty Temporal does not restore them). Leave it running, or stop with SP_ALLOW_TEMPORAL_RESET=1 after deciding about the comics listed by scripts/inspect-install.sh. API, worker and web are stopped."
  fi
  stop_proc temporal
  stop_proc s3
}

case "${1:-start}" in
  start) start_storage; start_temporal; start_app ;;
  start-app) start_app ;;
  stop) stop_all ;;
  stop-app) stop_app ;;
  status) for n in s3 temporal api worker web; do printf '%-9s %s\n' "$n" "$(alive "$n" && echo running || echo stopped)"; done
          echo "storage: $(storage_kind)" ;;
  *) die "usage: $0 start|stop|start-app|stop-app|status" ;;
esac
