#!/usr/bin/env bash
# Run every PromQL expression from the provisioned dashboards against a live
# Prometheus and fail if any query returns an error.
#
# Template variables are resolved the way Grafana does for "All":
#   $job, $instance, $container_id, $target -> .*   (all used with =~)
#   $__rate_interval -> 1m, $__interval -> 15s, $__range -> 1h
# Variable queries like label_values(<selector>, <label>) are checked by
# running count(<selector>).
set -euo pipefail

PROM_URL="${PROM_URL:-http://127.0.0.1:${PROMETHEUS_PORT:-9090}}"
DASH_DIR="${DASH_DIR:-$(dirname "$0")/../grafana/dashboards}"

# The \$ patterns are literal Grafana variables, not shell expansions.
# shellcheck disable=SC2016
resolve() {
  sed -e 's/\$__rate_interval/1m/g' -e 's/\$__interval/15s/g' -e 's/\$__range/1h/g' \
      -e 's/\$job/.*/g' -e 's/\$instance/.*/g' -e 's/\$container_id/.*/g' -e 's/\$target/.*/g'
}

total=0 failed=0 empty=0
while IFS=$'\t' read -r file title expr; do
  total=$((total + 1))
  resolved="$(printf '%s' "$expr" | resolve)"
  if [[ "$resolved" == *'$'* ]]; then
    echo "FAIL [$file] $title: unresolved variable in: $resolved"
    failed=$((failed + 1))
    continue
  fi
  resp="$(curl -sS --max-time 20 --data-urlencode "query=$resolved" "$PROM_URL/api/v1/query")" || resp='{"status":"error","error":"curl failed"}'
  status="$(jq -r '.status' <<<"$resp")"
  if [[ "$status" != "success" ]]; then
    echo "FAIL [$file] $title: $(jq -r '.error // "unknown error"' <<<"$resp")"
    echo "     query: $resolved"
    failed=$((failed + 1))
  elif [[ "$(jq '.data.result | length' <<<"$resp")" == "0" ]]; then
    echo "ok (no data yet) [$file] $title"
    empty=$((empty + 1))
  else
    echo "ok [$file] $title"
  fi
done < <(
  for f in "$DASH_DIR"/*.json; do
    name="$(basename "$f")"
    jq -r --arg f "$name" '
      (.panels[] | select(.targets) | .title as $t | .targets[] | [$f, $t, .expr]),
      (.templating.list[] | select(.type == "query") |
        [$f, "variable " + .name,
         (.definition | capture("^label_values\\((?<sel>.*), *[a-zA-Z_]+\\)$").sel | "count(" + . + ")")])
      | @tsv' "$f"
  done
)

echo "dashboard queries: $total checked, $failed failed, $empty returned no data (valid but empty)"
[[ "$failed" -eq 0 ]]
