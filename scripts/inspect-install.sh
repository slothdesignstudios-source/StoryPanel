#!/usr/bin/env bash
# Read-only report on a running local StoryPanel installation. Changes nothing, makes no paid call,
# prints no secret (for .env only the variable NAMES are listed, never values). Self-contained: it
# needs only bash, git and psql, plus the temporal CLI if present, and no checkout of the new version.
#
#   bash inspect-install.sh [install-dir]          default: ~/storypanel
set -uo pipefail
DIR="${1:-$HOME/storypanel}"
PGHOST="${PGHOST:-127.0.0.1}" PGPORT="${PGPORT:-5432}" PGUSER="${PGUSER:-storypanel}" DB="${DB:-storypanel}"
SP_RUN="${SP_RUN:-/tmp/storypanel-run}" SP_DATA="${SP_DATA:-/tmp/storypanel-data}"
TEMPORAL_BIN="${TEMPORAL_BIN:-$(command -v temporal || ls /tmp/temporal-sdk-python-* 2>/dev/null | head -1)}"
q() { psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DB" -X -q -At -F ' | ' \
        -c "set default_transaction_read_only = on" -c "$1" 2>&1; }
h() { printf '\n== %s\n' "$1"; }

h "installation checkout ($DIR)"
if git -C "$DIR" rev-parse --git-dir >/dev/null 2>&1; then
  echo "commit:  $(git -C "$DIR" rev-parse HEAD) ($(git -C "$DIR" log -1 --format='%cs %s' | cut -c1-80))"
  echo "branch:  $(git -C "$DIR" rev-parse --abbrev-ref HEAD)"
  echo "changed files (names only, .env excluded):"; git -C "$DIR" status --porcelain | grep -v ' \.env$' | sed 's/^/  /' | head -20
else
  echo "not a git checkout"
fi

h "processes started by scripts/native-stack.sh ($SP_RUN)"
for f in "$SP_RUN"/*.pid; do
  [ -f "$f" ] || continue; pid=$(cat "$f"); n=$(basename "$f" .pid)
  if kill -0 "$pid" 2>/dev/null; then
    echo "$n: pid $pid, up $(ps -o etime= -p "$pid" | tr -d ' '), cwd $(readlink "/proc/$pid/cwd" 2>/dev/null)"
    [ "$n" = temporal ] && { tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q -- '--db-filename' \
      && echo "  temporal: state kept on disk" || echo "  temporal: state IN MEMORY (running workflows are lost when it stops)"; }
    if [ "$n" = worker ] || [ "$n" = api ]; then
      tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | grep -E '^SP_(PROVIDER_MODE|FAKE_SCENARIO|FAL_ENABLED|VOYAGE_ENABLED|DATABASE_URL|TEMPORAL_ADDRESS|PRICING_FILE)=' \
        | sed -E 's#(postgresql[^:]*://[^:@]*):[^@]*@#\1:***@#; s/^/  /'
    fi
  else echo "$n: not running (stale pid $pid)"; fi
done
if command -v ss >/dev/null || command -v netstat >/dev/null; then
  echo "listening:"; (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -E ':(3000|8000|7233|9000|5432|6379)\b' | awk '{print "  "$4}' | sort -u
else echo "listening: (ss/netstat not installed: skipped)"; fi

h ".env (names only)"
if [ -f "$DIR/.env" ]; then
  echo "mode $(stat -c %a "$DIR/.env"); git-ignored: $(git -C "$DIR" check-ignore -q .env && echo yes || echo NO); tracked: $([ -n "$(git -C "$DIR" ls-files .env)" ] && echo YES || echo no)"
  grep -oE '^[A-Z_][A-Z0-9_]*=' "$DIR/.env" | tr -d '=' | sed 's/^/  set: /'
else echo "no .env"; fi

h "database $DB on $PGHOST:$PGPORT (read-only queries)"
echo "server_encoding: $(q 'show server_encoding')"
echo "schema revision: $(q 'select version_num from alembic_version')"
echo "size: $(q "select pg_size_pretty(pg_database_size(current_database()))")"
echo "comics by status (excluding deleted):"; q "select status, count(*) from projects where deleted_at is null group by 1 order by 1" | sed 's/^/  /'
echo "running or waiting comics (these need a live workflow):"
q "select id, status, coalesce(workflow_id,'-'), to_char(updated_at,'YYYY-MM-DD HH24:MI') from projects where deleted_at is null and status not in ('completed','partially_completed','failed','cancelled','blocked','expired') order by updated_at" | sed 's/^/  /'
echo "open credit holds: $(q "select count(*) || ' holds, ' || coalesce(sum(amount - settled),0) || ' credits' from credit_reservations where status = 'open'")"
echo "users: $(q 'select count(*) from users')"
echo "unresolved provider attempts (potential charges): $(q "select count(*) || ', USD ' || coalesce(sum(cost_counted_usd),0) from provider_attempts where cost_basis in ('reserved','unresolved')")"

h "Temporal (read-only listing)"
if [ -n "$TEMPORAL_BIN" ]; then
  if ! running=$("$TEMPORAL_BIN" workflow list --address 127.0.0.1:7233 --query 'ExecutionStatus="Running"' --limit 1000 2>&1); then
    echo "  Temporal at 127.0.0.1:7233 did not answer: cannot tell which workflows exist ($(echo "$running" | head -1 | cut -c1-120))"
  else
  echo "$running" | head -40 | sed 's/^/  /'
  waiting=$(q "select workflow_id from projects where deleted_at is null and workflow_id is not null and status not in ('completed','partially_completed','failed','cancelled','blocked','expired')")
  missing=$(comm -23 <(echo "$waiting" | sort -u | grep .) <(echo "$running" | awk '$1=="Running"{print $2}' | sort -u))
  echo "running or waiting comics: $(echo "$waiting" | grep -c .); with no running workflow now: $(echo "$missing" | grep -c .)"
  echo "$missing" | grep . | sed 's/^/  no workflow: /' | head -40
  fi
else echo "temporal CLI not found: skipped"; fi
echo "file-backed state: $([ -f "$SP_DATA/temporal.db" ] && echo "$SP_DATA/temporal.db exists" || echo none)"

h "space for a backup"
df -h "$HOME" | tail -1 | awk '{print "  free in $HOME: "$4}'
echo
echo "Nothing was changed. Send this output back as is (it contains no keys)."
