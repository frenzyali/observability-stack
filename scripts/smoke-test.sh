#!/usr/bin/env bash
# End-to-end test: bring the stack up, prove every part works, break the
# sample app, and prove the alerts travel all the way to a receiver.
#
#   scripts/smoke-test.sh            # full run, tears everything down at the end
#   KEEP_UP=1 scripts/smoke-test.sh  # leave the stack running afterwards
#
# Uses compose.test.yaml (shorter intervals and "for:" durations; same rules)
# so the run takes about 10 minutes. Exits non-zero if any step fails.
# Logs from a failed run are written to artifacts/.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PROJECT="obs-stack"
ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "note: $ENV_FILE not found, using .env.example (placeholder credentials)"
  ENV_FILE=.env.example
fi
set -a
# shellcheck disable=SC1090
. "./$ENV_FILE"
set +a

PROM="http://127.0.0.1:${PROMETHEUS_PORT:-9090}"
AM="http://127.0.0.1:${ALERTMANAGER_PORT:-9093}"
GRAFANA="http://127.0.0.1:${GRAFANA_PORT:-3000}"
APP="http://127.0.0.1:${SAMPLE_APP_PORT:-8000}"
export APP_URL="$APP"
GF_AUTH="${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASSWORD}"

compose() {
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f compose.yaml -f compose.test.yaml --profile demo "$@"
}

PASSED=0
FAILED=0
pass() { echo "PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "FAIL  $1${2:+ :: $2}"; FAILED=$((FAILED + 1)); }
step() { echo; echo "== $1"; }

# wait_for <timeout-seconds> <description> <command...>
wait_for() {
  local timeout="$1" desc="$2" start=$SECONDS
  shift 2
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS - start >= timeout )); then
      fail "$desc" "timed out after ${timeout}s"
      return 1
    fi
    sleep 5
  done
  pass "$desc ($((SECONDS - start))s)"
}

collect_logs() {
  mkdir -p artifacts
  compose ps -a > artifacts/compose-ps.txt 2>&1 || true
  compose logs --no-color --timestamps > artifacts/compose-logs.txt 2>&1 || true
  curl -fsS "$PROM/api/v1/targets" > artifacts/prometheus-targets.json 2>/dev/null || true
  curl -fsS "$PROM/api/v1/alerts" > artifacts/prometheus-alerts.json 2>/dev/null || true
  curl -fsS "$AM/api/v2/alerts" > artifacts/alertmanager-alerts.json 2>/dev/null || true
  echo "logs written to artifacts/"
}

cleanup() {
  local rc=$?
  if (( FAILED > 0 || rc != 0 )); then collect_logs; fi
  if [[ "${KEEP_UP:-0}" == "1" ]]; then
    echo "KEEP_UP=1: leaving the stack running (make down to stop it)"
  else
    step "Teardown"
    if compose down -v --remove-orphans >/dev/null 2>&1; then pass "stack removed (containers, network, volumes)"; else fail "teardown"; fi
  fi
  echo
  echo "Smoke test: $PASSED passed, $FAILED failed"
  if (( FAILED > 0 )); then echo "RESULT: FAIL"; exit 1; fi
  echo "RESULT: PASS"
}
trap cleanup EXIT

# --- helpers used as conditions ------------------------------------------------

all_targets_up() {
  local body
  body="$(curl -fsS "$PROM/api/v1/targets?state=active")" || return 1
  [[ "$(jq '.data.activeTargets | length' <<<"$body")" -gt 0 ]] &&
    [[ "$(jq '[.data.activeTargets[] | select(.health != "up")] | length' <<<"$body")" == "0" ]]
}

# am_alert_state <alertname> -> prints "active", "suppressed", ... or nothing
am_alert_state() {
  curl -fsS -G "$AM/api/v2/alerts" --data-urlencode "filter=alertname=\"$1\"" |
    jq -r '[.[].status.state] | unique | join(",")'
}
am_alert_active()  { [[ "$(am_alert_state "$1")" == *active* ]]; }
am_alert_gone()    { [[ -z "$(am_alert_state "$1")" ]]; }
prom_alert_gone()  {
  [[ "$(curl -fsS "$PROM/api/v1/alerts" | jq --arg a "$1" '[.data.alerts[] | select(.labels.alertname == $a)] | length')" == "0" ]]
}
alert_resolved()   { prom_alert_gone "$1" && am_alert_gone "$1"; }

# assert_fresh <job> <context>: the job's last scrape succeeded and is at most
# 2 scrape intervals old, so the alert state just asserted was computed from
# live data, not from samples left over before the scrapes stopped.
assert_fresh() {
  local res
  res="$(curl -fsS "$PROM/api/v1/targets?state=active" | jq -r --arg job "$1" '
    def secs: capture("^((?<h>[0-9]+)h)?((?<m>[0-9]+)m)?((?<s>[0-9]+)s)?$")
      | ((.h // "0") | tonumber) * 3600 + ((.m // "0") | tonumber) * 60 + ((.s // "0") | tonumber);
    [.data.activeTargets[] | select(.labels.job == $job)] | first // empty
    | (.lastScrape | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601) as $last
    | (.scrapeInterval | secs) as $iv
    | (now - $last | floor) as $age
    | "\(.health) \($age) \($iv)"')"
  local health age iv
  read -r health age iv <<<"$res"
  if [[ "$health" == "up" ]] && (( age <= 2 * iv )); then
    pass "$1 data fresh when $2 (last scrape ${age}s ago, interval ${iv}s)"
  else
    fail "$1 data fresh when $2" "health=${health:-missing} age=${age:-?}s limit=$((2 * ${iv:-0}))s"
  fi
}

# webhook_got <alertname> <firing|resolved>
webhook_got() {
  compose logs --no-color webhook-logger 2>/dev/null |
    sed -n 's/.*ALERT //p' |
    jq -e --arg a "$1" --arg s "$2" 'select(.alertname == $a and .status == $s)' >/dev/null
}

# --- 1. Bring up -------------------------------------------------------------

step "Start stack (test timings)"
for tool in docker curl jq; do
  command -v "$tool" >/dev/null || { fail "prerequisite $tool missing"; exit 1; }
done
if scripts/render-test-config.sh >/dev/null; then pass "rendered fast-timing test config"; else fail "render test config"; exit 1; fi
mkdir -p artifacts
# Start from empty volumes: leftover TSDB samples or Alertmanager's
# notification log from an earlier run would make the baseline and
# notification assertions meaningless.
if compose down -v --remove-orphans > artifacts/compose-down.log 2>&1; then
  pass "removed any previous obs-stack containers and volumes"
else
  fail "pre-test cleanup" "see artifacts/compose-down.log"
  exit 1
fi
if compose up -d --build --wait --wait-timeout 300 > artifacts/compose-up.log 2>&1; then
  pass "all containers started and healthy"
else
  tail -30 artifacts/compose-up.log
  fail "compose up --wait" "see artifacts/compose-up.log"
  exit 1
fi

unhealthy="$(compose ps --format '{{.Service}} {{.Health}}' | awk '$2 != "healthy"')"
if [[ -z "$unhealthy" ]]; then pass "every service reports a healthy healthcheck"; else fail "unhealthy services" "$unhealthy"; fi

# --- 2. Scraping -------------------------------------------------------------

step "Prometheus"
wait_for 120 "all Prometheus scrape targets are up" all_targets_up
targets="$(curl -fsS "$PROM/api/v1/targets?state=active" | jq '.data.activeTargets | length')"
echo "      ($targets active targets)"
rules_ok="$(curl -fsS "$PROM/api/v1/rules" | jq '[.data.groups[].rules[] | select(.health != "ok" and .health != "unknown")] | length')"
if [[ "$rules_ok" == "0" ]]; then pass "all recording and alerting rules evaluate without error"; else fail "rule health" "$rules_ok unhealthy rules"; fi

# --- 3. Grafana ----------------------------------------------------------------

step "Grafana"
if [[ "$(curl -fsS "$GRAFANA/api/health" | jq -r .database)" == "ok" ]]; then pass "Grafana /api/health database ok"; else fail "Grafana health"; fi
code="$(curl -s -o /dev/null -w '%{http_code}' "$GRAFANA/api/search")"
if [[ "$code" == "401" ]]; then pass "anonymous API access is rejected (401)"; else fail "anonymous access" "got HTTP $code"; fi
ds="$(curl -fsS -u "$GF_AUTH" "$GRAFANA/api/datasources/uid/prometheus" | jq -r '.type + " " + .url')"
if [[ "$ds" == "prometheus http://prometheus:9090" ]]; then pass "Prometheus datasource provisioned with fixed UID"; else fail "datasource" "$ds"; fi
if [[ "$(curl -fsS -u "$GF_AUTH" "$GRAFANA/api/datasources/uid/prometheus/health" | jq -r .status)" == "OK" ]]; then pass "datasource health check reaches Prometheus"; else fail "datasource health"; fi
expected="$(jq -r .uid grafana/dashboards/*.json | sort | tr '\n' ' ')"
got="$(curl -fsS -u "$GF_AUTH" "$GRAFANA/api/search?type=dash-db" | jq -r '.[].uid' | sort | tr '\n' ' ')"
if [[ "$expected" == "$got" ]]; then pass "all $(wc -w <<<"$expected") dashboards provisioned ($got)"; else fail "dashboards" "expected [$expected] got [$got]"; fi

# --- 4. Dashboard queries ---------------------------------------------------

step "Dashboard queries against live Prometheus"
sleep 20  # let the load generator produce a few scrapes of traffic
if PROM_URL="$PROM" scripts/check-dashboard-queries.sh > /tmp/obs-stack-queries.$$ 2>&1; then
  pass "$(tail -1 /tmp/obs-stack-queries.$$)"
else
  grep FAIL /tmp/obs-stack-queries.$$
  fail "dashboard queries" "$(tail -1 /tmp/obs-stack-queries.$$)"
fi
rm -f /tmp/obs-stack-queries.$$

# --- 5. Baseline -------------------------------------------------------------

step "Baseline: no alerts for the healthy app"
if am_alert_gone HighErrorRate; then pass "HighErrorRate not active at baseline"; else fail "HighErrorRate active before chaos"; fi
if am_alert_gone InstanceDown; then pass "InstanceDown not active at baseline"; else fail "InstanceDown active before chaos"; fi

# --- 6. Chaos: errors --------------------------------------------------------

step "Chaos 1: inject 80% errors"
if scripts/chaos.sh errors 0.8; then pass "fault injected"; else fail "chaos errors"; fi
wait_for 240 "HighErrorRate is firing (active) in Alertmanager" am_alert_active HighErrorRate &&
  assert_fresh sample-app "HighErrorRate fired"
wait_for 60 "webhook-logger received HighErrorRate firing notification" webhook_got HighErrorRate firing
if scripts/chaos.sh clear >/dev/null; then pass "errors cleared"; else fail "chaos clear"; fi

# --- 7. Chaos: app down ------------------------------------------------------

step "Chaos 2: stop the sample app"
if scripts/chaos.sh down >/dev/null 2>&1; then pass "sample-app stopped"; else fail "chaos down"; fi
wait_for 180 "InstanceDown is firing (active) in Alertmanager" am_alert_active InstanceDown
wait_for 60 "webhook-logger received InstanceDown firing notification" webhook_got InstanceDown firing
state="$(am_alert_state HighErrorRate)"
if [[ -z "$state" || "$state" == "suppressed" ]]; then pass "HighErrorRate inhibited by InstanceDown (state: ${state:-resolved})"; else fail "inhibition" "HighErrorRate state is $state"; fi

# --- 8. Recovery -------------------------------------------------------------

step "Recovery"
if scripts/chaos.sh recover >/dev/null 2>&1; then pass "sample-app restarted and healthy"; else fail "chaos recover"; fi
wait_for 180 "InstanceDown resolved in Prometheus and Alertmanager" alert_resolved InstanceDown
wait_for 60 "webhook-logger received InstanceDown resolved notification" webhook_got InstanceDown resolved
# The error samples must age out of the 5m rate window before the ratio drops.
wait_for 480 "HighErrorRate resolved in Prometheus and Alertmanager" alert_resolved HighErrorRate &&
  assert_fresh sample-app "HighErrorRate resolved"
wait_for 60 "webhook-logger received HighErrorRate resolved notification" webhook_got HighErrorRate resolved
wait_for 60 "all scrape targets up again" all_targets_up
