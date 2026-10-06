#!/usr/bin/env python3
"""Generate the provisioned Grafana dashboards from code.

    python3 grafana/generate.py            # write grafana/dashboards/*.json
    python3 grafana/generate.py --check    # exit 1 if the JSON is out of date

The JSON files are committed so Grafana can provision them directly; CI runs
--check so hand edits to the JSON cannot drift from this source.
Standard library only.
"""

import json
import pathlib
import sys

OUT = pathlib.Path(__file__).resolve().parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}  # fixed UID, see provisioning
SLO_BUDGET = 0.005  # 1 - 0.995


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------

def target(expr, legend="", ref="A", instant=False):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        t.update(instant=True, range=False)
    return t


def targets(*exprs):
    """exprs: (expr, legend) tuples -> targets with refIds A, B, C..."""
    return [target(e, l, chr(ord("A") + i)) for i, (e, l) in enumerate(exprs)]


def thresholds(*steps):
    """steps: (value or None, color) pairs, lowest first."""
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}


def timeseries(title, exprs, unit="short", w=12, h=8, description="", min_=None,
               max_=None, thresh=None, stack=False):
    defaults = {
        "unit": unit,
        "custom": {
            "drawStyle": "line", "lineWidth": 1, "fillOpacity": 10,
            "showPoints": "never", "spanNulls": False,
            "stacking": {"mode": "normal" if stack else "none", "group": "A"},
            "thresholdsStyle": {"mode": "line" if thresh else "off"},
        },
        "thresholds": thresh or thresholds((None, "green")),
    }
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    return {
        "type": "timeseries", "title": title, "description": description,
        "datasource": DS, "targets": targets(*exprs),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "_w": w, "_h": h,
    }


def stat(title, expr, unit="short", w=4, h=4, description="", thresh=None,
         legend="", decimals=None, mappings=None, color_mode="value"):
    defaults = {"unit": unit, "thresholds": thresh or thresholds((None, "green")),
                "color": {"mode": "thresholds"}, "mappings": mappings or []}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat", "title": title, "description": description,
        "datasource": DS, "targets": [target(expr, legend, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": color_mode, "graphMode": "none", "textMode": "auto",
                    "justifyMode": "auto", "orientation": "auto"},
        "_w": w, "_h": h,
    }


def gauge(title, expr, unit="percentunit", w=6, h=6, description="", thresh=None,
          legend="", min_=0, max_=1):
    return {
        "type": "gauge", "title": title, "description": description,
        "datasource": DS, "targets": [target(expr, legend, instant=True)],
        "fieldConfig": {"defaults": {"unit": unit, "min": min_, "max": max_,
                                     "thresholds": thresh or thresholds((None, "green"))},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "showThresholdLabels": False, "showThresholdMarkers": True},
        "_w": w, "_h": h,
    }


def table(title, expr, w=24, h=8, description="", unit="short", exclude=()):
    hidden = {name: True for name in ("Time", "__name__") + tuple(exclude)}
    return {
        "type": "table", "title": title, "description": description,
        "datasource": DS, "targets": [{**target(expr, instant=True), "format": "table"}],
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": thresholds((None, "green"))},
                        "overrides": []},
        "options": {"showHeader": True, "cellHeight": "sm"},
        "transformations": [{"id": "organize", "options": {"excludeByName": hidden}}],
        "_w": w, "_h": h,
    }


def row(title):
    return {"type": "row", "title": title, "collapsed": False, "panels": [], "_w": 24, "_h": 1}


def query_var(name, label, query, multi=True, include_all=True):
    return {
        "name": name, "label": label, "type": "query", "datasource": DS,
        "query": {"query": query, "refId": f"{name}-var"}, "definition": query,
        "refresh": 2, "sort": 1, "multi": multi, "includeAll": include_all,
        "allValue": ".*", "current": {}, "options": [], "hide": 0, "regex": "",
    }


def layout(panels):
    """Assign ids and gridPos left-to-right, wrapping at 24 columns."""
    x = y = row_h = 0
    out = []
    for pid, p in enumerate(panels, start=1):
        w, h = p.pop("_w"), p.pop("_h")
        if x + w > 24 or p["type"] == "row":
            x, y = 0, y + row_h
            row_h = 0
        p["id"] = pid
        p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        out.append(p)
        x += w
        row_h = max(row_h, h)
        if p["type"] == "row":
            x, y, row_h = 0, y + h, 0
    return out


def dashboard(uid, title, description, tags, variables, panels, refresh="30s",
              time_from="now-1h"):
    return {
        "uid": uid, "title": title, "description": description, "tags": tags,
        "editable": False, "graphTooltip": 1, "schemaVersion": 41, "version": 1,
        "refresh": refresh, "time": {"from": time_from, "to": "now"},
        "timezone": "browser", "fiscalYearStartMonth": 0, "liveNow": False,
        "templating": {"list": variables},
        "annotations": {"list": [{
            "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
            "name": "Annotations & Alerts", "type": "dashboard"}]},
        "links": [], "panels": layout(panels),
    }


PCT_WARN = thresholds((None, "green"), (0.8, "orange"), (0.9, "red"))
UPDOWN = [{"type": "value", "options": {"0": {"text": "DOWN", "color": "red"},
                                        "1": {"text": "UP", "color": "green"}}}]


# --------------------------------------------------------------------------
# Dashboards
# --------------------------------------------------------------------------

def node_overview():
    sel = 'job=~"$job",instance=~"$instance"'
    fs = f'{sel},fstype!~"tmpfs|ramfs|overlay|squashfs|nsfs|fuse.*",mountpoint!~"/boot.*"'
    return dashboard(
        "node-overview", "Node overview",
        "Host CPU, memory, disk and load from node-exporter.",
        ["node", "infrastructure"],
        [query_var("job", "Job", "label_values(node_uname_info, job)"),
         query_var("instance", "Instance", 'label_values(node_uname_info{job=~"$job"}, instance)')],
        [
            stat("Uptime", f"time() - node_boot_time_seconds{{{sel}}}", "s", legend="{{instance}}"),
            stat("CPU cores", f'count by (instance) (node_cpu_seconds_total{{{sel},mode="idle"}})',
                 legend="{{instance}}"),
            stat("Memory total", f"node_memory_MemTotal_bytes{{{sel}}}", "bytes", legend="{{instance}}"),
            gauge("CPU used", f"instance:node_cpu_utilisation:rate5m{{{sel}}}", w=4, h=4,
                  thresh=PCT_WARN, legend="{{instance}}"),
            gauge("Memory used", f"instance:node_memory_utilisation:ratio{{{sel}}}", w=4, h=4,
                  thresh=PCT_WARN, legend="{{instance}}"),
            gauge("Root disk used", f'1 - instance_mountpoint:node_filesystem_avail_bytes:ratio{{{sel},mountpoint="/"}}',
                  w=4, h=4, thresh=PCT_WARN, legend="{{instance}}"),
            row("CPU and memory"),
            timeseries("CPU utilisation", [(f"instance:node_cpu_utilisation:rate5m{{{sel}}}", "{{instance}}")],
                       "percentunit", min_=0, max_=1,
                       thresh=thresholds((None, "green"), (0.9, "red")),
                       description="1 - idle CPU share (5m rate). HostHighCPU fires above 90% for 10m."),
            timeseries("CPU by mode", [(f'sum by (mode) (rate(node_cpu_seconds_total{{{sel},mode!="idle"}}[$__rate_interval]))', "{{mode}}")],
                       "short", stack=True, description="CPU seconds per second, all cores, excluding idle."),
            timeseries("Memory utilisation", [(f"instance:node_memory_utilisation:ratio{{{sel}}}", "{{instance}}")],
                       "percentunit", min_=0, max_=1, thresh=thresholds((None, "green"), (0.9, "red")),
                       description="1 - MemAvailable/MemTotal. HostHighMemory fires above 90% for 10m."),
            timeseries("Load average", [(f"node_load1{{{sel}}}", "1m {{instance}}"),
                                        (f"node_load5{{{sel}}}", "5m {{instance}}"),
                                        (f"node_load15{{{sel}}}", "15m {{instance}}")]),
            row("Disk"),
            timeseries("Filesystem space used", [(f"1 - instance_mountpoint:node_filesystem_avail_bytes:ratio{{{sel}}}", "{{mountpoint}}")],
                       "percentunit", min_=0, max_=1, thresh=thresholds((None, "green"), (0.9, "red")),
                       description="HostDiskSpaceLow fires below 10% free."),
            timeseries("Free space, 24h linear prediction", [
                (f"node_filesystem_avail_bytes{{{fs}}}", "now {{mountpoint}}"),
                (f"predict_linear(node_filesystem_avail_bytes{{{fs}}}[6h], 24 * 3600)", "in 24h {{mountpoint}}")],
                "bytes", description="HostDiskWillFillIn24h fires when the prediction drops below zero."),
            timeseries("Disk I/O", [
                (f"sum by (instance) (rate(node_disk_read_bytes_total{{{sel}}}[$__rate_interval]))", "read {{instance}}"),
                (f"sum by (instance) (rate(node_disk_written_bytes_total{{{sel}}}[$__rate_interval]))", "write {{instance}}")],
                "Bps", w=24),
        ])


def container_overview():
    sel = 'container_id=~"$container_id"'
    return dashboard(
        "container-overview", "Container overview",
        "Per-container CPU, memory, network and restarts from cAdvisor. Containers are "
        "identified by short ID (map to names with `docker ps`); the Docker socket is "
        "intentionally not mounted.",
        ["containers", "infrastructure"],
        [query_var("container_id", "Container ID", 'label_values(container_memory_working_set_bytes{container_id!=""}, container_id)')],
        [
            stat("Containers", 'count(container_memory_working_set_bytes{container_id!=""})', w=6),
            stat("Total container CPU", f"sum(rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))",
                 "short", w=6, decimals=2, description="CPU cores in use across selected containers."),
            stat("Total container memory", f"sum(container_memory_working_set_bytes{{{sel}}})", "bytes", w=6),
            stat("Restarts (15m)", f"sum(changes(container_start_time_seconds{{{sel}}}[15m]))", w=6,
                 thresh=thresholds((None, "green"), (1, "orange"), (3, "red"))),
            timeseries("CPU by container", [(f"sum by (container_id) (rate(container_cpu_usage_seconds_total{{{sel}}}[$__rate_interval]))", "{{container_id}}")],
                       "short", description="CPU cores used."),
            timeseries("Memory working set by container", [(f"container_memory_working_set_bytes{{{sel}}}", "{{container_id}}")], "bytes"),
            timeseries("Memory used vs limit", [(f"container_memory_working_set_bytes{{{sel}}} / (container_spec_memory_limit_bytes{{{sel}}} > 0)", "{{container_id}}")],
                       "percentunit", min_=0, max_=1, thresh=thresholds((None, "green"), (0.9, "red")),
                       description="Only containers with a memory limit. ContainerHighMemory fires above 90%."),
            timeseries("Block I/O by container", [
                (f"sum by (container_id) (rate(container_fs_reads_bytes_total{{{sel}}}[$__rate_interval]))", "read {{container_id}}"),
                (f"-sum by (container_id) (rate(container_fs_writes_bytes_total{{{sel}}}[$__rate_interval]))", "write {{container_id}}")],
                "Bps", description="Read positive, write negative. Per-container network metrics need the "
                                   "Docker API, which this stack does not expose to cAdvisor."),
            table("Container restarts in the last hour",
                  f"sort_desc(changes(container_start_time_seconds{{{sel}}}[1h]))",
                  exclude=("id", "instance", "job")),
        ])


def sample_app_red():
    sel = 'job=~"$job",instance=~"$instance"'
    b = SLO_BUDGET
    return dashboard(
        "sample-app-red", "Sample app: RED and SLO",
        "Rate, errors and duration of the sample Flask app, with its 99.5% availability SLO "
        "(7-day window) and error budget.",
        ["app", "red", "slo"],
        [query_var("job", "Job", "label_values(http_requests_total, job)"),
         query_var("instance", "Instance", 'label_values(http_requests_total{job=~"$job"}, instance)')],
        [
            row("Service level objective: 99.5% of requests succeed (7 days)"),
            stat("Availability (7d)", '1 - job:slo_errors_per_request:ratio_rate7d{job="sample-app"}',
                 "percentunit", w=6, decimals=3,
                 thresh=thresholds((None, "red"), (0.995, "green")),
                 description="Share of non-5xx requests over the SLO window. Target 99.5%."),
            stat("Error budget remaining (7d)", f'1 - job:slo_errors_per_request:ratio_rate7d{{job="sample-app"}} / {b}',
                 "percentunit", w=6, decimals=1,
                 thresh=thresholds((None, "red"), (0.25, "orange"), (0.5, "green")),
                 description="100% = no errors yet; 0% = budget spent; negative = SLO breached."),
            stat("Burn rate (1h)", f'job:slo_errors_per_request:ratio_rate1h{{job="sample-app"}} / {b}',
                 "short", w=6, decimals=2,
                 thresh=thresholds((None, "green"), (1, "orange"), (6, "red")),
                 description="1 = budget spent exactly over 7 days. Pages at 14.4 (1h) or 6 (6h)."),
            stat("Firing alerts (app)", 'count(ALERTS{alertstate="firing",job="sample-app"}) or vector(0)',
                 w=6, thresh=thresholds((None, "green"), (1, "red"))),
            timeseries("Error budget burn rate by window", [
                (f'job:slo_errors_per_request:ratio_rate5m{{job="sample-app"}} / {b}', "5m"),
                (f'job:slo_errors_per_request:ratio_rate30m{{job="sample-app"}} / {b}', "30m"),
                (f'job:slo_errors_per_request:ratio_rate1h{{job="sample-app"}} / {b}', "1h"),
                (f'job:slo_errors_per_request:ratio_rate6h{{job="sample-app"}} / {b}', "6h"),
                (f'job:slo_errors_per_request:ratio_rate1d{{job="sample-app"}} / {b}', "1d")],
                "short", w=24, thresh=thresholds((None, "green"), (6, "orange"), (14.4, "red")),
                description="Multi-window burn rates used by SLOErrorBudgetBurnFast/Slow. Lines at 6x and 14.4x."),
            row("Rate, errors, duration"),
            stat("Requests/s", f"sum(rate(http_requests_total{{{sel}}}[5m]))", "reqps", w=8, decimals=2),
            stat("Error ratio (5m)", f'sum(rate(http_requests_total{{{sel},status=~"5.."}}[5m])) / sum(rate(http_requests_total{{{sel}}}[5m]))',
                 "percentunit", w=8, decimals=2, thresh=thresholds((None, "green"), (0.01, "orange"), (0.05, "red"))),
            stat("p95 latency (5m)", f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[5m])))",
                 "s", w=8, decimals=3, thresh=thresholds((None, "green"), (0.5, "orange"), (1, "red"))),
            timeseries("Requests by status", [(f"sum by (status) (rate(http_requests_total{{{sel}}}[$__rate_interval]))", "{{status}}")],
                       "reqps", stack=True),
            timeseries("Requests by route", [(f"sum by (path) (rate(http_requests_total{{{sel}}}[$__rate_interval]))", "{{path}}")], "reqps"),
            timeseries("Error ratio (5xx)", [(f"job_instance:http_requests_errors:ratio_rate5m{{{sel}}}", "{{instance}}")],
                       "percentunit", min_=0, thresh=thresholds((None, "green"), (0.05, "red")),
                       description="HighErrorRate fires above 5% for 2m (with at least 0.1 req/s)."),
            timeseries("Latency percentiles", [
                (f"histogram_quantile(0.50, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[$__rate_interval])))", "p50"),
                (f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[$__rate_interval])))", "p95"),
                (f"histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[$__rate_interval])))", "p99")],
                "s", thresh=thresholds((None, "green"), (1, "red")),
                description="HighLatencyP95 fires when p95 stays above 1s for 5m."),
            timeseries("p95 latency by route", [(f"histogram_quantile(0.95, sum by (le, path) (rate(http_request_duration_seconds_bucket{{{sel}}}[$__rate_interval])))", "{{path}}")],
                       "s", w=24),
        ], refresh="10s", time_from="now-30m")


def blackbox_uptime():
    sel = 'instance=~"$target"'
    return dashboard(
        "blackbox-uptime", "Blackbox uptime",
        "External HTTP checks by blackbox-exporter: is each endpoint answering 2xx, and how fast.",
        ["blackbox", "uptime"],
        [query_var("target", "Target", 'label_values(probe_success{job="blackbox-http"}, instance)')],
        [
            stat("Status", f'probe_success{{job="blackbox-http",{sel}}}', w=24, h=4, legend="{{instance}}",
                 thresh=thresholds((None, "red"), (1, "green")), mappings=UPDOWN, color_mode="background"),
            table("Uptime over selected range",
                  f'avg_over_time(probe_success{{job="blackbox-http",{sel}}}[$__range])',
                  w=12, unit="percentunit", exclude=("job",)),
            table("Last HTTP status code", f'probe_http_status_code{{job="blackbox-http",{sel}}}',
                  w=12, exclude=("job",)),
            timeseries("Probe success", [(f'probe_success{{job="blackbox-http",{sel}}}', "{{instance}}")],
                       "short", w=24, min_=0, max_=1,
                       description="1 = 2xx received. ProbeFailed fires after 2m at 0."),
            timeseries("Probe duration", [(f'probe_duration_seconds{{job="blackbox-http",{sel}}}', "{{instance}}")], "s"),
            timeseries("Probe phases (sum of targets)", [
                (f'sum by (phase) (probe_http_duration_seconds{{job="blackbox-http",{sel}}})', "{{phase}}")],
                "s", stack=True),
        ])


def self_health():
    return dashboard(
        "monitoring-self-health", "Prometheus and Alertmanager health",
        "Who watches the watchers: scrape health, TSDB, rule evaluation and notification delivery.",
        ["prometheus", "alertmanager", "meta"],
        [query_var("job", "Job", "label_values(up, job)")],
        [
            stat("Targets up", 'sum(up{job=~"$job"})', w=6, thresh=thresholds((None, "green"))),
            stat("Targets down", 'count(up{job=~"$job"} == 0) or vector(0)', w=6,
                 thresh=thresholds((None, "green"), (1, "red"))),
            stat("Firing alerts", 'count(ALERTS{alertstate="firing"}) or vector(0)', w=6,
                 thresh=thresholds((None, "green"), (1, "red"))),
            stat("Head series", 'prometheus_tsdb_head_series{job="prometheus"}', w=6,
                 description="Active series in memory. Watch this for cardinality growth."),
            table("Scrape targets", 'up{job=~"$job"}', w=12),
            timeseries("Scrape duration", [('scrape_duration_seconds{job=~"$job"}', "{{job}} {{instance}}")], "s"),
            row("Prometheus"),
            timeseries("Samples ingested/s", [('rate(prometheus_tsdb_head_samples_appended_total{job="prometheus"}[$__rate_interval])', "samples")], "short"),
            timeseries("Rule group evaluation time", [('prometheus_rule_group_last_duration_seconds{job="prometheus"}', "{{rule_group}}")], "s"),
            timeseries("Rule evaluation failures/s", [('sum by (rule_group) (rate(prometheus_rule_evaluation_failures_total{job="prometheus"}[$__rate_interval]))', "{{rule_group}}")],
                       "short", min_=0),
            timeseries("TSDB storage size", [('prometheus_tsdb_storage_blocks_bytes{job="prometheus"} + prometheus_tsdb_wal_storage_size_bytes{job="prometheus"}', "blocks + WAL")], "bytes"),
            row("Alertmanager"),
            timeseries("Alerts in Alertmanager", [('sum by (state) (alertmanager_alerts{job="alertmanager"})', "{{state}}")], "short"),
            timeseries("Notifications sent and failed/s", [
                ('sum by (integration) (rate(alertmanager_notifications_total{job="alertmanager"}[$__rate_interval]))', "sent {{integration}}"),
                ('sum by (integration) (rate(alertmanager_notifications_failed_total{job="alertmanager"}[$__rate_interval]))', "failed {{integration}}")],
                "short", min_=0),
            timeseries("Notification latency p95", [('histogram_quantile(0.95, sum by (le, integration) (rate(alertmanager_notification_latency_seconds_bucket{job="alertmanager"}[$__rate_interval])))', "{{integration}}")],
                       "s", w=24),
        ])


DASHBOARDS = {
    "node-overview.json": node_overview,
    "container-overview.json": container_overview,
    "sample-app-red.json": sample_app_red,
    "blackbox-uptime.json": blackbox_uptime,
    "monitoring-self-health.json": self_health,
}


def main():
    check = "--check" in sys.argv[1:]
    stale = []
    for name, build in DASHBOARDS.items():
        text = json.dumps(build(), indent=2, sort_keys=True) + "\n"
        path = OUT / name
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)
            print(f"wrote {path.relative_to(OUT.parent.parent)}")
    if stale:
        print("out of date (run python3 grafana/generate.py): " + ", ".join(stale))
        return 1
    if check:
        print(f"{len(DASHBOARDS)} dashboards up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
