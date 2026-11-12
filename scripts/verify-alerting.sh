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
# With ALERTMANAGER_SECRETS_DIR pointing at real receiver URLs (PagerDuty/Slack relay), the local
# sink sees nothing; delivery is then confirmed from Alertmanager's own counters (receiver answered
# 2xx, no failures) and by the person on call acknowledging the page/ticket.
sink_has() {
  if [ -n "${ALERTMANAGER_SECRETS_DIR:-}" ]; then
    local status alert receiver; status=$(echo "$1" | grep -oE '"status": "[a-z]+"' | cut -d'"' -f4)
    receiver=$(echo "$1" | grep -oE '"receiver": "[a-z]+"' | cut -d'"' -f4)
    am_delivered "$receiver" && [ "$(am_failed)" = "0" ] || return 1
    read -r -p "Did the $receiver receiver show the $status notification? [y/N] " a; [ "$a" = y ]
  else
    docker compose --profile observability logs alert-sink 2>/dev/null | grep -q "$1"
  fi
}
am_metric() { curl -s http://127.0.0.1:9093/metrics | grep -E "^$1" | awk '{s+=$2} END {print s+0}'; }
# summed over every integration (webhook locally; pagerduty and slack with the production config)
am_delivered() { [ "$(am_metric 'alertmanager_notifications_total\{')" != "0" ]; }
am_failed() { am_metric 'alertmanager_notifications_failed_total\{'; }
wait_for() { local what="$1" pattern="$2" limit="$3"; for _ in $(seq 1 "$limit"); do sink_has "$pattern" && { log "received: $what"; return 0; }; sleep 10; done; log "NOT received: $what"; return 1; }
: > "$OUT"
docker compose --profile observability up -d --no-build --scale worker=2 prometheus alertmanager alert-sink worker >/dev/null
sleep 90
log "worker targets up: $(curl -sg 'http://127.0.0.1:9090/api/v1/query?query=sum(up{job="worker"})' | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["result"][0]["value"][1])')"
W=($(docker compose ps -q worker))
log "stopping one of ${#W[@]} workers"; docker stop -t 5 "${W[0]}" >/dev/null
wait_for "WorkerInstanceDown firing -> ticket" '"receiver": "ticket", "status": "firing", "alertname": "WorkerInstanceDown"' 50
log "stopping the other worker"; docker stop -t 5 "${W[1]}" >/dev/null
wait_for "WorkerDown firing -> page" '"receiver": "page", "status": "firing", "alertname": "WorkerDown"' 40
log "restarting workers"; docker start "${W[@]}" >/dev/null
wait_for "WorkerDown resolved -> page" '"receiver": "page", "status": "resolved", "alertname": "WorkerDown"' 40
# WorkerInstanceDown already resolved when the last worker stopped (no live series); WorkerDown covers that case.
wait_for "WorkerInstanceDown resolved -> ticket" '"receiver": "ticket", "status": "resolved", "alertname": "WorkerInstanceDown"' 5
docker compose --profile observability logs --no-log-prefix alert-sink >> "$OUT"
log "alertmanager notifications sent: $(am_metric 'alertmanager_notifications_total\{'), failed: $(am_failed)"
log "done"
