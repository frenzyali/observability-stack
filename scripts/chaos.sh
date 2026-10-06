#!/usr/bin/env bash
# Inject failures into the sample app so alerts fire.
#
#   scripts/chaos.sh errors [ratio]    # make a share of requests return 503 (default 0.8)
#   scripts/chaos.sh latency [secs]    # add delay to every request (default 1.5)
#   scripts/chaos.sh down              # stop the sample-app container
#   scripts/chaos.sh up                # start it again
#   scripts/chaos.sh clear             # remove injected errors and latency
#   scripts/chaos.sh recover           # start the app and clear all faults
#   scripts/chaos.sh demo              # errors -> wait -> down -> wait -> recover
#
# Only touches the sample-app container of the obs-stack Compose project.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="obs-stack"
APP_URL="${APP_URL:-http://127.0.0.1:${SAMPLE_APP_PORT:-8000}}"
compose() { docker compose -p "$PROJECT" "$@"; }

need_loadgen() {
  # Alerts on error ratio and latency need traffic; start the demo load generator.
  if ! compose --profile demo ps --status running --services | grep -qx loadgen; then
    echo "chaos: starting load generator (profile demo)"
    compose --profile demo up -d --wait loadgen >/dev/null
  fi
}

set_chaos() {
  curl -fsS --max-time 5 -X POST -H 'Content-Type: application/json' -d "$1" "$APP_URL/chaos"
  echo
}

wait_app_healthy() {
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "$APP_URL/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  echo "chaos: sample-app did not become healthy" >&2
  return 1
}

cmd="${1:-}"
case "$cmd" in
  errors)
    need_loadgen
    echo "chaos: injecting errors (ratio ${2:-0.8})"
    set_chaos "{\"error_ratio\": ${2:-0.8}}"
    ;;
  latency)
    need_loadgen
    echo "chaos: injecting ${2:-1.5}s latency"
    set_chaos "{\"extra_latency_seconds\": ${2:-1.5}}"
    ;;
  down)
    echo "chaos: stopping sample-app"
    compose stop sample-app
    ;;
  up)
    echo "chaos: starting sample-app"
    compose start sample-app
    wait_app_healthy
    ;;
  clear)
    echo "chaos: clearing injected faults"
    curl -fsS --max-time 5 -X DELETE "$APP_URL/chaos"
    echo
    ;;
  recover)
    compose start sample-app
    wait_app_healthy
    curl -fsS --max-time 5 -X DELETE "$APP_URL/chaos" >/dev/null
    echo "chaos: sample-app running, faults cleared"
    ;;
  demo)
    "$0" errors
    echo "chaos: watch http://127.0.0.1:${ALERTMANAGER_PORT:-9093} and 'docker compose logs -f webhook-logger'"
    echo "chaos: waiting 3 minutes for HighErrorRate (production timings)..."
    sleep 180
    "$0" clear
    "$0" down
    echo "chaos: waiting 2 minutes for InstanceDown..."
    sleep 120
    "$0" recover
    ;;
  *)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
