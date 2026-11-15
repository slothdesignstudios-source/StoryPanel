#!/usr/bin/env bash
# Read-only report on a local StoryPanel installation. Changes nothing, makes no paid call, contacts
# no external service, and prints no secret: from process environments and .env it prints only
# variable NAMES or yes/no, never a database URL, password or key. Needs bash, git, psql, python3,
# curl, and the temporal CLI (for workflow status).
#
#   bash inspect-install.sh [install-dir]          default: ~/storypanel
set -uo pipefail
DIR="${1:-$HOME/storypanel}"
SP_RUN="${SP_RUN:-/tmp/storypanel-run}" SP_DATA="${SP_DATA:-/tmp/storypanel-data}"
TEMPORAL_BIN="${TEMPORAL_BIN:-$(command -v temporal || ls /tmp/temporal-sdk-python-* 2>/dev/null | head -1)}"
h() { printf '\n== %s\n' "$1"; }
pidof_run() { local f="$SP_RUN/$1.pid"; [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null && cat "$f"; }

# Settings the app processes really use (worker first, then API), read from /proc. The python helper
# returns shell assignments for internal use only; nothing it reads is printed.
settings() {  # $1 pid -> shell assignments
  python3 - "$1" <<'PY'
import shlex, sys, urllib.parse
try:
    raw = open(f"/proc/{sys.argv[1]}/environ", "rb").read().split(b"\0")
except OSError:
    sys.exit(0)
env = dict(x.decode(errors="replace").split("=", 1) for x in raw if b"=" in x)
out = {"HAS_DB_URL": "yes" if env.get("SP_DATABASE_URL") else "no"}
url = env.get("SP_DATABASE_URL")
if url:
    u = urllib.parse.urlsplit(url.replace("+asyncpg", "").replace("+psycopg", ""))
    out.update(PGHOST=u.hostname or "", PGPORT=str(u.port or 5432), PGUSER=urllib.parse.unquote(u.username or ""),
               PGPASSWORD=urllib.parse.unquote(u.password or ""), PGDATABASE=u.path.lstrip("/"))
for k in ("SP_PROVIDER_MODE", "SP_FAL_ENABLED", "SP_VOYAGE_ENABLED", "SP_TEMPORAL_ADDRESS", "SP_TEMPORAL_NAMESPACE",
          "SP_S3_ENDPOINT_URL", "SP_S3_BUCKET", "SP_PRICING_FILE"):
    out["CFG_" + k] = env.get(k, "")
out["CFG_FAKE_SCENARIO_SET"] = "yes" if env.get("SP_FAKE_SCENARIO") else "no"
print("\n".join(f"{k}={shlex.quote(v)}" for k, v in out.items()))
PY
}

h "installation checkout ($DIR)"
if git -C "$DIR" rev-parse --git-dir >/dev/null 2>&1; then
  echo "commit:  $(git -C "$DIR" rev-parse HEAD) ($(git -C "$DIR" log -1 --format='%cs %s' | cut -c1-80))"
  echo "branch:  $(git -C "$DIR" rev-parse --abbrev-ref HEAD)"
  echo "local changes (names only, .env excluded):"
  git -C "$DIR" status --porcelain | grep -v ' \.env$' | sed 's/^/  /' | head -30
else echo "not a git checkout"; fi

h "processes started by scripts/native-stack.sh ($SP_RUN)"
for f in "$SP_RUN"/*.pid; do
  [ -f "$f" ] || { echo "no pid files"; break; }
  n=$(basename "$f" .pid); pid=$(cat "$f")
  if kill -0 "$pid" 2>/dev/null; then
    echo "$n: running (pid $pid, up $(ps -o etime= -p "$pid" | tr -d ' '))"
    case "$n" in
      temporal) tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q -- '--db-filename' \
                  && echo "  state: kept on disk" || echo "  state: IN MEMORY (lost when it stops)" ;;
      s3) tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q moto_server \
                  && echo "  storage: moto S3 stand-in, IN MEMORY (stored images are lost when it stops)" ;;
    esac
  else echo "$n: not running (stale pid file)"; fi
done
CFG_PID=$(pidof_run worker || pidof_run api || true)
HAS_DB_URL=no PGHOST=127.0.0.1 PGPORT=5432 PGUSER=storypanel PGPASSWORD="" PGDATABASE=storypanel
CFG_SP_PROVIDER_MODE="" CFG_SP_FAL_ENABLED="" CFG_SP_VOYAGE_ENABLED="" CFG_SP_TEMPORAL_ADDRESS="" CFG_SP_TEMPORAL_NAMESPACE=""
CFG_SP_S3_ENDPOINT_URL="" CFG_SP_S3_BUCKET="" CFG_SP_PRICING_FILE="" CFG_FAKE_SCENARIO_SET=no
if [ -n "$CFG_PID" ]; then
  eval "$(settings "$CFG_PID")"
  echo "settings of the running $( [ "$(pidof_run worker)" = "$CFG_PID" ] && echo worker || echo API) (values that are not secret; others yes/no):"
  echo "  SP_DATABASE_URL configured: $HAS_DB_URL"
  echo "  SP_PROVIDER_MODE: ${CFG_SP_PROVIDER_MODE:-unset (fake)}; SP_FAKE_SCENARIO set: $CFG_FAKE_SCENARIO_SET"
  echo "  SP_FAL_ENABLED: ${CFG_SP_FAL_ENABLED:-unset}; SP_VOYAGE_ENABLED: ${CFG_SP_VOYAGE_ENABLED:-unset}"
  echo "  SP_TEMPORAL_ADDRESS: ${CFG_SP_TEMPORAL_ADDRESS:-unset (127.0.0.1:7233)}; SP_TEMPORAL_NAMESPACE: ${CFG_SP_TEMPORAL_NAMESPACE:-unset (default)}"
  echo "  SP_S3_BUCKET: ${CFG_SP_S3_BUCKET:-unset (storypanel-assets)}; SP_PRICING_FILE: ${CFG_SP_PRICING_FILE:-unset}"
else
  echo "no running worker or API: using the local defaults for the checks below"
fi
export PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE
TADDR="${CFG_SP_TEMPORAL_ADDRESS:-127.0.0.1:7233}" TNS="${CFG_SP_TEMPORAL_NAMESPACE:-default}"
BUCKET="${CFG_SP_S3_BUCKET:-storypanel-assets}" S3URL="${CFG_SP_S3_ENDPOINT_URL:-}"

h ".env (names only)"
if [ -f "$DIR/.env" ]; then
  echo "mode $(stat -c %a "$DIR/.env"); git-ignored: $(git -C "$DIR" check-ignore -q .env && echo yes || echo NO); tracked: $([ -n "$(git -C "$DIR" ls-files .env 2>/dev/null)" ] && echo YES || echo no)"
  grep -oE '^[A-Z_][A-Z0-9_]*=' "$DIR/.env" | tr -d '=' | sed 's/^/  set: /'
else echo "no .env"; fi

h "database (read-only queries)"
q() { psql -X -q -At -F ' | ' -c "set default_transaction_read_only = on" -c "$1" 2>&1; }
if ! psql -X -q -At -c "select 1" >/dev/null 2>&1; then
  echo "database: NOT reachable with the configured settings; the checks below are unknown"
  DB_OK=no
else
  DB_OK=yes
  echo "server_encoding: $(q 'show server_encoding'); collation: $(q 'select datcollate from pg_database where datname = current_database()')"
  echo "schema revision: $(q 'select version_num from alembic_version')"
  echo "size: $(q "select pg_size_pretty(pg_database_size(current_database()))")"
  echo "comics by status (excluding deleted):"; q "select status, count(*) from projects where deleted_at is null group by 1 order by 1" | sed 's/^/  /'
  echo "open credit holds: $(q "select count(*) || ' holds, ' || coalesce(sum(amount - settled),0) || ' credits' from credit_reservations where status = 'open'")"
  echo "users: $(q 'select count(*) from users'); stored assets (rows): $(q 'select count(*) from assets')"
  echo "unresolved provider attempts (potential charges): $(q "select count(*) || ', USD ' || coalesce(sum(cost_counted_usd),0) from provider_attempts where cost_basis in ('reserved','unresolved')")"
fi

h "stored images (local S3 stand-in only; nothing external is contacted)"
case "$S3URL" in
  http://127.0.0.1:*|http://localhost:*)
    python3 - "$S3URL" "$BUCKET" <<'PY' || echo "listing failed: object count unknown"
import sys, urllib.parse, urllib.request, xml.etree.ElementTree as ET
base, bucket = sys.argv[1].rstrip("/"), sys.argv[2]
ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
count = size = 0; token = None
while True:
    q = {"list-type": "2", "max-keys": "1000", **({"continuation-token": token} if token else {})}
    with urllib.request.urlopen(f"{base}/{bucket}?{urllib.parse.urlencode(q)}", timeout=20) as r:
        root = ET.fromstring(r.read())
    for c in root.iter(f"{ns}Contents"):
        count += 1; size += int(c.findtext(f"{ns}Size") or 0)
    if (root.findtext(f"{ns}IsTruncated") or "false") != "true":
        break
    token = root.findtext(f"{ns}NextContinuationToken")
print(f"objects in bucket {bucket}: {count} ({size / 1e6:.1f} MB), all pages listed")
PY
    if [ "$DB_OK" = yes ]; then
      missing=0; checked=0
      while read -r key; do
        [ -n "$key" ] || continue; checked=$((checked+1))
        code=$(curl -s -o /dev/null -w '%{http_code}' -I "${S3URL%/}/$BUCKET/$key")
        [ "$code" = 200 ] || missing=$((missing+1))
      done < <(q "(select storage_key from assets order by created_at asc limit 10) union all (select storage_key from assets order by created_at desc limit 10)")
      echo "sample of asset rows (10 oldest, 10 newest): $checked checked, $missing missing from storage"
    fi ;;
  "") echo "S3 endpoint not known (no running worker/API): skipped" ;;
  *) echo "S3 endpoint is not local: skipped (this script contacts no external service)" ;;
esac

h "Temporal workflows (address $TADDR, namespace $TNS; structured output)"
if [ -z "$TEMPORAL_BIN" ]; then
  echo "temporal CLI not found: workflow status UNKNOWN for every comic"
elif ! health=$("$TEMPORAL_BIN" operator cluster health --address "$TADDR" -o json 2>&1) \
     || ! echo "$health" | grep -q '"SERVING"' \
     || ! "$TEMPORAL_BIN" operator namespace describe --namespace "$TNS" --address "$TADDR" -o json >/dev/null 2>&1; then
  echo "Temporal or namespace not reachable: workflow status UNKNOWN for every comic (nothing is reported as lost)"
elif [ "$DB_OK" != yes ]; then
  echo "database not reachable: comics unknown"
else
  running=0 lost=0 unknown=0
  while IFS='|' read -r pid status wid; do
    pid=$(echo "$pid" | tr -d ' '); status=$(echo "$status" | tr -d ' '); wid=$(echo "$wid" | tr -d ' ')
    [ -n "$wid" ] || continue
    if out=$("$TEMPORAL_BIN" workflow describe --workflow-id "$wid" --namespace "$TNS" --address "$TADDR" -o json 2>&1); then
      st=$(echo "$out" | python3 -c "import json,sys; print(json.load(sys.stdin)['workflowExecutionInfo']['status'])" 2>/dev/null)
      case "$st" in
        WORKFLOW_EXECUTION_STATUS_RUNNING) running=$((running+1)); verdict="running" ;;
        WORKFLOW_EXECUTION_STATUS_*) lost=$((lost+1)); verdict="LOST (workflow ${st#WORKFLOW_EXECUTION_STATUS_})" ;;
        *) unknown=$((unknown+1)); verdict="UNKNOWN (unreadable answer)" ;;
      esac
    elif echo "$out" | grep -q "workflow not found for ID"; then
      lost=$((lost+1)); verdict="LOST (workflow not found)"
    else
      unknown=$((unknown+1)); verdict="UNKNOWN ($(echo "$out" | head -1 | cut -c1-80))"
    fi
    echo "  $pid  $status  $verdict"
  done < <(q "select id, status, workflow_id from projects where deleted_at is null and workflow_id is not null and status not in ('completed','partially_completed','failed','cancelled','blocked','expired') order by updated_at")
  echo "running or waiting comics: $((running+lost+unknown)); workflow running: $running; lost: $lost; unknown: $unknown"
fi
echo "file-backed Temporal state: $([ -f "$SP_DATA/temporal.db" ] && echo "$SP_DATA/temporal.db exists" || echo none)"

h "space for backups"
df -h "$HOME" | tail -1 | awk '{print "  free in $HOME: "$4}'
echo
echo "Nothing was changed. Send this output back as is (it contains no keys or database URL)."
