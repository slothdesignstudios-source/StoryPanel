#!/usr/bin/env bash
# Upgrade verification with representative pre-upgrade data (simulated providers, no paid calls).
#
#   scripts/upgrade-verify.sh [old-commit]        default old commit: c6a45b1 (migrations up to 0011)
#
# 1. The OLD code (a git worktree) runs on a throwaway database, its own Temporal dev server, S3 (moto)
#    and Redis DB, and creates comics paused at the cast gate, paused at the script gate, and completed.
# 2. Their workflow histories are saved and replayed against THIS checkout's workflow code.
# 3. The database is migrated to head with this checkout; row counts are compared.
# 4. This checkout's API and worker take over the SAME database and Temporal server, report the legacy
#    rows honestly, finish the paused comics, and the credit ledger is reconciled.
# 5. SQL_ASCII -> UTF-8: the pre-upgrade data is copied into a SQL_ASCII database (like a cluster created
#    under a C locale), then moved to a new UTF-8 database with pg_dump/pg_restore and migrated.
#
# Only databases named storypanel_upgrade* are created or dropped. Nothing else is touched.
# Needs: scripts/native-services.sh running (Postgres + Redis), the temporal CLI. Evidence: docs/evidence/upgrade/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OLD_REF="${1:-c6a45b1}"
WORK="${SP_UPGRADE_WORK:-/tmp/storypanel-upgrade}"
OUT="${SP_UPGRADE_OUT:-$ROOT/docs/evidence/upgrade}"
PGPORT="${PGPORT:-5432}"
PSQL=(psql -h 127.0.0.1 -p "$PGPORT" -U storypanel -v ON_ERROR_STOP=1 -q)
DB=storypanel_upgrade
NEWPY="$ROOT/services/backend/.venv/bin/python"
TEMPORAL_BIN="${TEMPORAL_BIN:-$(command -v temporal || ls /tmp/temporal-sdk-python-* 2>/dev/null | head -1)}"
mkdir -p "$WORK" "$OUT"
rm -rf "$OUT/histories"

export SP_ENV=local SP_PROVIDER_MODE=fake SP_AUTH_MODE=dev SP_LOG_LEVEL=WARNING SP_SIGNUP_BONUS_CREDITS=200
export SP_DATABASE_URL="postgresql+asyncpg://storypanel@127.0.0.1:$PGPORT/$DB"
export SP_REDIS_URL=redis://127.0.0.1:6379/5
export SP_S3_ENDPOINT_URL=http://127.0.0.1:9010 SP_S3_ACCESS_KEY_ID=local SP_S3_SECRET_ACCESS_KEY=local
export SP_TEMPORAL_ADDRESS=127.0.0.1:7244 SP_CORS_ORIGINS='["http://localhost:3000"]' NO_PROXY=127.0.0.1,localhost

launch() { local name="$1"; shift; nohup "$@" >"$WORK/$name.log" 2>&1 & echo $! >"$WORK/$name.pid"; }
halt() { for n in "$@"; do [ -f "$WORK/$n.pid" ] && kill "$(cat "$WORK/$n.pid")" 2>/dev/null || true; rm -f "$WORK/$n.pid"; done; sleep 2; }
wait_http() { for _ in $(seq 1 90); do curl -fsS -o /dev/null "$1" 2>/dev/null && return 0; sleep 1; done; echo "timeout: $1" >&2; return 1; }
fresh_db() {  # $1 name (must start with storypanel_upgrade), $2 encoding
  case "$1" in storypanel_upgrade*) ;; *) echo "refusing to touch database $1" >&2; exit 1 ;; esac
  "${PSQL[@]}" -d postgres -c "drop database if exists $1" -c "create database $1 encoding '$2' template template0"
  "${PSQL[@]}" -d "$1" -c "create extension if not exists vector"
}
counts() {  # row count of every table, sorted
  "${PSQL[@]}" -d "$1" -Atc "select string_agg(format('select %L as t, count(*) as n from %I', tablename, tablename), ' union all ' order by tablename) from pg_tables where schemaname='public' and tablename <> 'alembic_version'" \
    | "${PSQL[@]}" -d "$1" -At -F' ' | sort
}
trap 'halt old-api old-worker new-api new-worker temporal s3' EXIT
halt old-api old-worker new-api new-worker temporal s3

echo "== services (own Temporal :7244, S3 :9010, Redis db 5, database $DB)"
fresh_db "$DB" UTF8
redis-cli -n 5 flushdb >/dev/null
(cd "$ROOT/services/backend" && launch s3 uv run moto_server -H 127.0.0.1 -p 9010)
launch temporal "$TEMPORAL_BIN" server start-dev --ip 127.0.0.1 --port 7244 --headless --log-level warn
sleep 4

echo "== old code: $OLD_REF"
[ -d "$WORK/old" ] || git -C "$ROOT" worktree add --detach "$WORK/old" "$OLD_REF" >/dev/null
OLDB="$WORK/old/services/backend"
(cd "$OLDB" && uv sync --frozen -q && uv run alembic upgrade head >"$WORK/old-migrate.log" 2>&1 && uv run alembic current 2>/dev/null | tail -1) | tee "$OUT/old-revision.txt"
(cd "$OLDB" && launch old-api uv run uvicorn storypanel.api.app:app_factory --factory --host 127.0.0.1 --port 8010)
(cd "$OLDB" && SP_METRICS_PORT=9475 launch old-worker uv run python -m storypanel.workflows.worker)
wait_http http://127.0.0.1:8010/readyz
"$NEWPY" "$ROOT/scripts/upgrade_verify.py" seed --api http://127.0.0.1:8010 --state "$WORK/state.json"
"$NEWPY" "$ROOT/scripts/upgrade_verify.py" history --temporal 127.0.0.1:7244 --state "$WORK/state.json" --histories "$OUT/histories"
halt old-api old-worker
counts "$DB" >"$WORK/rows-before.txt"
"${PSQL[@]}" -d "$DB" -Atc "select credit_balance, credit_reserved from users where email = 'upgrade-check@example.com'" >"$WORK/credits-before.txt"
pg_dump -h 127.0.0.1 -p "$PGPORT" -U storypanel -Fc --no-owner -f "$WORK/pre-upgrade.dump" "$DB"

echo "== replay pre-upgrade histories against this checkout"
"$NEWPY" "$ROOT/scripts/upgrade_verify.py" replay --histories "$OUT/histories" --state "$WORK/state.json" --report "$OUT/replay.json"

echo "== migrate to head with this checkout"
(cd "$ROOT/services/backend" && uv run alembic upgrade head >"$OUT/migrate.log" 2>&1 && uv run alembic current 2>/dev/null | tail -1) | tee "$OUT/new-revision.txt"
counts "$DB" >"$WORK/rows-after.txt"
# every table that existed keeps its rows; tables added by the migration start empty
{
  if join -a1 "$WORK/rows-before.txt" "$WORK/rows-after.txt" | awk 'NF != 3 || $2 != $3 { bad = 1 } END { exit bad }'; then
    echo "PASS every pre-upgrade table keeps its row count"
  else
    echo "FAIL row counts changed"; join -a1 "$WORK/rows-before.txt" "$WORK/rows-after.txt"
  fi
  join -v2 "$WORK/rows-before.txt" "$WORK/rows-after.txt" | awk '{ print "added by the migration: " $1 " (" $2 " rows)" }'
  echo "-- rows after the migration"; cat "$WORK/rows-after.txt"
} | tee "$OUT/rows.txt"
grep -q '^FAIL' "$OUT/rows.txt" && exit 1

echo "== this checkout takes over the same database and Temporal server"
(cd "$ROOT/services/backend" && launch new-api uv run uvicorn storypanel.api.app:app_factory --factory --host 127.0.0.1 --port 8010)
(cd "$ROOT/services/backend" && SP_METRICS_PORT=9476 launch new-worker uv run python -m storypanel.workflows.worker)
wait_http http://127.0.0.1:8010/readyz
"$NEWPY" "$ROOT/scripts/upgrade_verify.py" finish --api http://127.0.0.1:8010 --state "$WORK/state.json" --report "$OUT/finish.json" \
  --credits-before "$(cat "$WORK/credits-before.txt")"
(cd "$ROOT/services/backend" && uv run python - <<'EOF') | tee "$OUT/reconcile.txt"
import asyncio
from storypanel.db.session import transaction
from storypanel.services import credits
async def main():
    async with transaction() as db:
        problems = await credits.reconcile(db)
    print("PASS ledger reconciles (cached balances = ledger, holds = open reservations)" if not problems else f"FAIL {problems}")
asyncio.run(main())
EOF
halt new-api new-worker

echo "== SQL_ASCII -> UTF-8 (on copies; the pre-upgrade data at revision $(cat "$OUT/old-revision.txt"))"
A=storypanel_upgrade_ascii U=storypanel_upgrade_utf8
fresh_db "$A" SQL_ASCII
pg_restore -h 127.0.0.1 -p "$PGPORT" -U storypanel --no-owner -d "$A" "$WORK/pre-upgrade.dump" 2>"$WORK/restore-ascii.log" || true
# text a person typed, stored by the app as UTF-8 bytes (allowed in a SQL_ASCII text column)
"${PSQL[@]}" -d "$A" -c "update projects set prompt = 'Café on the pier: ñandú, Ærø, «olé», 漫画' where id = (select id from projects order by created_at limit 1)"
{
  echo "server_encoding of the SQL_ASCII copy: $("${PSQL[@]}" -d "$A" -Atc 'show server_encoding')"
  if "${PSQL[@]}" -d "$A" -c "update characters set spec = jsonb_set(spec, '{name}', '\"Zo\\u00eb\"'::jsonb) where id = (select id from characters limit 1)" 2>"$WORK/ascii-jsonb.err"; then
    echo "UNEXPECTED: non-ASCII JSON accepted by SQL_ASCII"
  else
    echo "PASS the SQL_ASCII database rejects non-ASCII JSON: $(head -1 "$WORK/ascii-jsonb.err")"
  fi
  # 1. back up (custom format), 2. create a NEW UTF-8 database, 3. restore into it
  pg_dump -h 127.0.0.1 -p "$PGPORT" -U storypanel -Fc --no-owner -f "$WORK/ascii.dump" "$A"
  fresh_db "$U" UTF8
  pg_restore -h 127.0.0.1 -p "$PGPORT" -U storypanel --no-owner --exit-on-error -d "$U" "$WORK/ascii.dump" 2>"$WORK/restore-utf8.log" \
    && echo "PASS restore into UTF-8 (invalid bytes would stop the restore with an error, never be dropped silently)"
  # 4. compare
  [ "$(counts "$A")" = "$(counts "$U")" ] && echo "PASS every table has the same row count in the UTF-8 copy"
  got=$("${PSQL[@]}" -d "$U" -Atc "select prompt from projects order by created_at limit 1")
  [ "$got" = 'Café on the pier: ñandú, Ærø, «olé», 漫画' ] && echo "PASS non-ASCII text arrives intact: $got"
  # 5. migrate the UTF-8 copy to head; JSON with non-ASCII text now works
  (cd "$ROOT/services/backend" && SP_DATABASE_URL="postgresql+asyncpg://storypanel@127.0.0.1:$PGPORT/$U" uv run alembic upgrade head >"$WORK/migrate-utf8.log" 2>&1) \
    && echo "PASS the UTF-8 copy migrates to $(cd "$ROOT/services/backend" && SP_DATABASE_URL="postgresql+asyncpg://storypanel@127.0.0.1:$PGPORT/$U" uv run alembic current 2>/dev/null | tail -1)"
  "${PSQL[@]}" -d "$U" -c "update characters set spec = jsonb_set(spec, '{name}', '\"Zo\\u00eb\"'::jsonb) where id = (select id from characters limit 1)" \
    && echo "PASS the UTF-8 database stores non-ASCII JSON"
  echo "server_encoding of the new copy: $("${PSQL[@]}" -d "$U" -Atc 'show server_encoding')"
} | tee "$OUT/sql-ascii.txt"
"${PSQL[@]}" -d postgres -c "drop database if exists $A" -c "drop database if exists $U"
echo "evidence: $OUT"
