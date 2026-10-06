"""Sample Flask service instrumented with prometheus_client.

Exposes RED metrics (rate, errors, duration) for every request except
/healthz and /metrics, so probes and scrapes do not skew the SLO.

A small fault-injection API under /chaos lets scripts/chaos.sh add an error
ratio and extra latency at runtime without restarting the process.
"""

import os
import random
import threading
import time

from flask import Flask, abort, g, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

app = Flask(__name__)

REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled, by route template and status code.",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, by route template.",
    ["method", "path"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

UNINSTRUMENTED = {"/healthz", "/metrics"}
CHAOS_ENABLED = os.environ.get("CHAOS_ENABLED", "false").lower() == "true"

_chaos_lock = threading.Lock()
_chaos = {"error_ratio": 0.0, "extra_latency_seconds": 0.0}


def _route_label():
    # Use the route template (e.g. "/slow"), never the raw URL, so unknown
    # paths cannot create unbounded label values.
    if request.url_rule is None:
        return "unmatched"
    return request.url_rule.rule


@app.before_request
def _start_timer():
    g.start = time.perf_counter()
    if request.path in UNINSTRUMENTED or request.path.startswith("/chaos"):
        return None
    with _chaos_lock:
        error_ratio = _chaos["error_ratio"]
        extra = _chaos["extra_latency_seconds"]
    if extra:
        time.sleep(extra)
    if error_ratio and random.random() < error_ratio:
        abort(503)
    return None


@app.after_request
def _record(response):
    if request.path in UNINSTRUMENTED or request.path.startswith("/chaos"):
        return response
    path = _route_label()
    REQUESTS.labels(request.method, path, str(response.status_code)).inc()
    LATENCY.labels(request.method, path).observe(time.perf_counter() - g.start)
    return response


@app.get("/")
def index():
    return jsonify(message="hello from the sample app")


@app.get("/slow")
def slow():
    time.sleep(random.uniform(0.1, 0.5))
    return jsonify(message="that took a while")


@app.get("/error")
def error():
    abort(500)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/chaos", methods=["GET", "POST", "DELETE"])
def chaos():
    if not CHAOS_ENABLED:
        abort(404)
    with _chaos_lock:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            try:
                error_ratio = float(body.get("error_ratio", _chaos["error_ratio"]))
                latency = float(body.get("extra_latency_seconds", _chaos["extra_latency_seconds"]))
            except (TypeError, ValueError):
                abort(400)
            if not 0.0 <= error_ratio <= 1.0 or not 0.0 <= latency <= 5.0:
                abort(400)
            _chaos.update(error_ratio=error_ratio, extra_latency_seconds=latency)
        elif request.method == "DELETE":
            _chaos.update(error_ratio=0.0, extra_latency_seconds=0.0)
        return jsonify(_chaos)
