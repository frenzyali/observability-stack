#!/usr/bin/env bash
# Render a fast-timing copy of the Prometheus and Alertmanager config into
# .test/ for the end-to-end smoke test (mounted by compose.test.yaml).
#
# Only timings change. Every alert expression, threshold, label and route is
# byte-for-byte the same as production:
#   - scrape/evaluation interval 15s -> 5s
#   - every alert "for:" duration    -> 15s
#   - Alertmanager group_wait 30s -> 5s, group_interval 5m -> 15s
set -euo pipefail
cd "$(dirname "$0")/.."

out=.test
rm -rf "$out"
mkdir -p "$out/prometheus/rules" "$out/alertmanager"

sed -e 's/^  scrape_interval: 15s/  scrape_interval: 5s/' \
    -e 's/^  scrape_timeout: 10s/  scrape_timeout: 4s/' \
    -e 's/^  evaluation_interval: 15s/  evaluation_interval: 5s/' \
    prometheus/prometheus.yml > "$out/prometheus/prometheus.yml"

for f in prometheus/rules/*.yml; do
  sed -E 's/^( +)for: [0-9]+[smhd]$/\1for: 15s/' "$f" > "$out/prometheus/rules/$(basename "$f")"
done

sed -e 's/^  group_wait: 30s /  group_wait: 5s  /' \
    -e 's/^  group_interval: 5m /  group_interval: 15s/' \
    alertmanager/alertmanager.yml > "$out/alertmanager/alertmanager.yml"

# Fail loudly if a substitution silently stopped matching.
grep -q '^  scrape_interval: 5s' "$out/prometheus/prometheus.yml"
grep -q '^  evaluation_interval: 5s' "$out/prometheus/prometheus.yml"
grep -q '^  group_wait: 5s' "$out/alertmanager/alertmanager.yml"
grep -q '^  group_interval: 15s' "$out/alertmanager/alertmanager.yml"
if grep -rEn '^ +for: ' "$out/prometheus/rules" | grep -v 'for: 15s$'; then
  echo "render-test-config: some 'for:' durations were not shortened" >&2
  exit 1
fi
echo "rendered test config into $out/"
