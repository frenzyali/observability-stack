#!/usr/bin/env bash
# Static checks. Every tool runs as a throwaway, pinned container (nothing to
# install except Docker, jq and python3), as the current user, with the repo
# mounted read-only and no network unless the tool needs it.
#
#   scripts/lint.sh            # all checks
#   scripts/lint.sh trivy      # one check by name (see the CHECKS list)
#
# Prints PASS/FAIL per check and exits non-zero if any check fails.
# Check functions are called indirectly as "check_$name".
# shellcheck disable=SC2329
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PROMETHEUS_IMAGE="prom/prometheus:v3.15.0"
ALERTMANAGER_IMAGE="prom/alertmanager:v0.34.1"
PYTHON_IMAGE="python:3.14.8-slim-trixie"
HADOLINT_IMAGE="hadolint/hadolint:v2.15.1"
SHELLCHECK_IMAGE="koalaman/shellcheck-alpine:v0.11.0"
GITLEAKS_IMAGE="zricethezav/gitleaks:v8.30.1"
TRIVY_IMAGE="aquasec/trivy:0.75.0"
YAMLLINT_VERSION="1.38.0"
APP_IMAGE="obs-stack/sample-app:local"

RUN=(docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/repo:ro" -w /repo)
OFFLINE=("${RUN[@]}" --network none)

check_compose() {
  docker compose --env-file .env.example -f compose.yaml config -q &&
    scripts/render-test-config.sh >/dev/null &&
    docker compose --env-file .env.example -f compose.yaml -f compose.test.yaml config -q &&
    # Every published port must bind to loopback only.
    ! docker compose --env-file .env.example -f compose.yaml --profile demo config --format json |
      jq -e '[.services[].ports[]? | select(.host_ip != "127.0.0.1")] | length > 0' >/dev/null
}

check_promtool_config() {
  docker run --rm --network none -v "$PWD/prometheus:/etc/prometheus:ro" \
    --entrypoint promtool "$PROMETHEUS_IMAGE" check config /etc/prometheus/prometheus.yml
}

check_promtool_rules() {
  "${OFFLINE[@]}" --entrypoint promtool "$PROMETHEUS_IMAGE" check rules prometheus/rules/*.yml
}

check_promtool_test() {
  local tests=()
  mapfile -t tests < <(cd prometheus/tests && ls -- *.yml)
  "${OFFLINE[@]}" -w /repo/prometheus/tests --entrypoint promtool "$PROMETHEUS_IMAGE" \
    test rules "${tests[@]}"
}

# Every alert defined in prometheus/rules must appear in a unit test with
# both a firing case (exp_alerts with labels) and a non-firing case ([]).
check_alert_coverage() {
  local missing=0 alert
  while read -r alert; do
    if ! awk -v a="$alert" '
          $0 ~ "alertname: "a"$" {seen=1; next}
          seen && /exp_alerts: \[\]/ {empty=1; seen=0}
          seen && /exp_alerts:$/ {firing=1; seen=0}
          END {exit !(empty && firing)}' prometheus/tests/*.yml; then
      echo "alert $alert lacks a firing and/or non-firing unit test"
      missing=1
    fi
  done < <(grep -h '^ *- alert:' prometheus/rules/*.yml | awk '{print $3}' | sort -u)
  [[ $missing -eq 0 ]] && echo "every alert has firing and non-firing unit tests"
}

check_amtool() {
  "${OFFLINE[@]}" --entrypoint amtool "$ALERTMANAGER_IMAGE" check-config alertmanager/alertmanager.yml
}

check_yamllint() {
  docker run --rm -v "$PWD:/repo:ro" -w /repo "$PYTHON_IMAGE" \
    sh -c "pip install --quiet --root-user-action=ignore yamllint==$YAMLLINT_VERSION && yamllint --strict ."
}

check_dashboards() {
  local f rc=0
  for f in grafana/dashboards/*.json; do
    jq empty "$f" || rc=1
    # Every panel must use the provisioned datasource UID.
    jq -e '[.panels[] | select(.datasource != null and .datasource.uid != "prometheus")] | length == 0' "$f" >/dev/null ||
      { echo "$f: panel with unexpected datasource"; rc=1; }
  done
  python3 grafana/generate.py --check || rc=1
  return "$rc"
}

check_hadolint() {
  "${OFFLINE[@]}" "$HADOLINT_IMAGE" hadolint app/Dockerfile
}

check_shellcheck() {
  "${OFFLINE[@]}" "$SHELLCHECK_IMAGE" shellcheck scripts/*.sh
}

check_gitleaks() {
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "no commits yet"; return 1
  fi
  "${RUN[@]}" "$GITLEAKS_IMAGE" git /repo --redact --verbose --no-banner
}

# Scan the sample-app image from a `docker save` tarball, so Trivy never
# needs the Docker socket. Full HIGH/CRITICAL report for the record; fail only
# on CRITICAL findings that have a fix available (see .trivyignore for
# accepted risks).
check_trivy() {
  local cache="${TRIVY_CACHE_DIR:-$HOME/.cache/trivy}" tar
  mkdir -p "$cache" artifacts
  docker compose --env-file .env.example build sample-app >/dev/null || return 1
  tar="$(mktemp -d)/sample-app.tar"
  docker save "$APP_IMAGE" -o "$tar" || return 1
  local trivy=(docker run --rm --user "$(id -u):$(id -g)" -v "$cache:/cache" -v "$(dirname "$tar"):/scan:ro"
               -v "$PWD/.trivyignore:/repo/.trivyignore:ro" -w /repo "$TRIVY_IMAGE")
  "${trivy[@]}" image --cache-dir /cache --quiet --input /scan/sample-app.tar \
    --severity HIGH,CRITICAL --format table --exit-code 0 | tee artifacts/trivy-report.txt
  "${trivy[@]}" image --cache-dir /cache --quiet --input /scan/sample-app.tar \
    --severity CRITICAL --ignore-unfixed --ignorefile /repo/.trivyignore --exit-code 1
  local rc=$?
  rm -rf "$(dirname "$tar")"
  return "$rc"
}

CHECKS=(compose promtool_config promtool_rules promtool_test alert_coverage amtool
        yamllint dashboards hadolint shellcheck gitleaks trivy)
if [[ $# -gt 0 ]]; then CHECKS=("$@"); fi

declare -a RESULTS=()
failed=0
for c in "${CHECKS[@]}"; do
  echo "== $c"
  if "check_$c"; then
    RESULTS+=("PASS  $c")
  else
    RESULTS+=("FAIL  $c")
    failed=1
  fi
  echo
done

echo "== Summary"
printf '%s\n' "${RESULTS[@]}"
exit $failed
