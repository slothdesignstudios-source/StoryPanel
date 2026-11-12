#!/usr/bin/env bash
# Alert delivery drill on the Compose stack with two workers:
#   one worker stopped  -> WorkerInstanceDown (ticket) reaches the ticket receiver
#   both workers stopped -> WorkerDown (page) reaches the page receiver
#   workers restarted    -> both resolve, and the resolution is delivered
# Uses the real rules (infra/observability/alerts.yml) and routing (alertmanager.yml); the
# receivers are the local alert-sink. Takes ~15 minutes because of the rules' `for:` windows.
# Writes docs/evidence/alerting-drill.txt.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=docs/evidence/alerting-drill.txt
log() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$OUT"; }
sink_has() { docker compose --profile observability logs alert-sink 2>/dev/null | grep -q "$1"; }
wait_for() { local what="$1" pattern="$2" limit="$3"; for _ in $(seq 1 "$limit"); do sink_has "$pattern" && { log "received: $what"; return 0; }; sleep 10; done; log "NOT received: $what"; return 1; }
: > "$OUT"
docker compose --profile observability up -d --no-build --scale worker=2 prometheus alertmanager alert-sink worker >/dev/null
sleep 40
log "worker targets up: $(curl -s 'http://127.0.0.1:9090/api/v1/query?query=sum(up{job="worker"})' | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["result"][0]["value"][1])')"
W=($(docker compose ps -q worker))
log "stopping one of ${#W[@]} workers"; docker stop "${W[0]}" >/dev/null
wait_for "WorkerInstanceDown firing -> ticket" '"receiver": "ticket", "status": "firing", "alertname": "WorkerInstanceDown"' 50
log "stopping the other worker"; docker stop "${W[1]}" >/dev/null
wait_for "WorkerDown firing -> page" '"receiver": "page", "status": "firing", "alertname": "WorkerDown"' 40
log "restarting workers"; docker start "${W[@]}" >/dev/null
wait_for "WorkerDown resolved -> page" '"receiver": "page", "status": "resolved", "alertname": "WorkerDown"' 40
wait_for "WorkerInstanceDown resolved -> ticket" '"receiver": "ticket", "status": "resolved", "alertname": "WorkerInstanceDown"' 40
docker compose --profile observability logs --no-log-prefix alert-sink >> "$OUT"
log "done"
