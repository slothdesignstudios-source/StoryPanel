#!/usr/bin/env bash
# Starts Postgres (with pgvector) and Redis natively, for machines without Docker.
# Usage: scripts/native-services.sh start|stop   (data lives in $SP_DATA, default /tmp/storypanel-data;
# set SP_DATA to a persistent directory to keep the database, e.g. for reconciling a live run later)
# Works as root (runs Postgres as the postgres user) or as a normal user (e.g. WSL); the Unix socket is
# kept in the data directory, so /var/run/postgresql need not be writable. Clients connect over TCP.
set -euo pipefail
SP_DATA="${SP_DATA:-/tmp/storypanel-data}"
# newest installed PostgreSQL (16 on Ubuntu 24.04, newer on later releases) unless PGBIN is set
PGBIN="${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
[ -x "$PGBIN/initdb" ] || { echo "PostgreSQL server binaries not found; install postgresql (or set PGBIN)" >&2; exit 1; }
PGPORT="${PGPORT:-5432}"
run_pg() { if [ "$(id -u)" = "0" ]; then runuser -u postgres -- "$@"; else "$@"; fi; }
case "${1:-start}" in
  start)
    mkdir -p "$SP_DATA/pg" "$SP_DATA/redis"
    [ "$(id -u)" = "0" ] && chown -R postgres "$SP_DATA/pg"
    if [ ! -f "$SP_DATA/pg/PG_VERSION" ]; then run_pg "$PGBIN/initdb" -D "$SP_DATA/pg" -U storypanel --auth=trust >/dev/null; fi
    run_pg "$PGBIN/pg_ctl" -D "$SP_DATA/pg" -o "-p $PGPORT -c listen_addresses=127.0.0.1 -c max_connections=200 -c unix_socket_directories=$SP_DATA/pg" -l "$SP_DATA/pg/log" -w start || true
    for db in storypanel storypanel_test; do
      psql -h 127.0.0.1 -p "$PGPORT" -U storypanel -d postgres -tc "select 1 from pg_database where datname='$db'" | grep -q 1 \
        || psql -h 127.0.0.1 -p "$PGPORT" -U storypanel -d postgres -c "create database $db" >/dev/null
      psql -h 127.0.0.1 -p "$PGPORT" -U storypanel -d "$db" -c "create extension if not exists vector" >/dev/null
    done
    redis-cli -p 6379 ping >/dev/null 2>&1 || redis-server --port 6379 --dir "$SP_DATA/redis" --daemonize yes --save "" >/dev/null
    echo "postgres :$PGPORT and redis :6379 are up" ;;
  stop)
    run_pg "$PGBIN/pg_ctl" -D "$SP_DATA/pg" -m fast stop || true
    redis-cli -p 6379 shutdown nosave || true ;;
esac
