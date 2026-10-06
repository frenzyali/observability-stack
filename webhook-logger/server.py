"""Tiny Alertmanager webhook receiver that logs every notification.

Each alert is printed as one JSON line prefixed with "ALERT " so tests can
grep the container log. The full payload is printed too. Standard library
only, no external dependencies.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "9095"))
MAX_BODY = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path == "/healthz":
            self._reply(200, b'{"status":"ok"}')
        else:
            self._reply(404, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            self._reply(400, b'{"error":"bad length"}')
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._reply(400, b'{"error":"invalid json"}')
            return
        receiver = self.path.strip("/") or "default"
        for alert in payload.get("alerts", []):
            line = {
                "receiver": receiver,
                "status": alert.get("status"),
                "alertname": alert.get("labels", {}).get("alertname"),
                "severity": alert.get("labels", {}).get("severity"),
                "instance": alert.get("labels", {}).get("instance"),
                "summary": alert.get("annotations", {}).get("summary"),
            }
            print("ALERT " + json.dumps(line, sort_keys=True), flush=True)
        print("PAYLOAD " + json.dumps(payload, sort_keys=True), flush=True)
        self._reply(200, b'{"status":"received"}')

    def log_message(self, fmt, *args):
        pass  # keep the log to alert lines only

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"webhook-logger listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
