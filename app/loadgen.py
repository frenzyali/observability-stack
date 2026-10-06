"""Steady synthetic traffic for the sample app so dashboards have data.

Roughly 88% "/", 10% "/slow" and 2% "/error" at TARGET_RPS requests/second.
Uses only the standard library.
"""

import os
import random
import time
import urllib.error
import urllib.request

TARGET = os.environ.get("TARGET_URL", "http://sample-app:8000").rstrip("/")
RPS = float(os.environ.get("TARGET_RPS", "10"))
HEARTBEAT = os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat")
WEIGHTS = (("/", 88), ("/slow", 10), ("/error", 2))


def pick_path():
    return random.choices([p for p, _ in WEIGHTS], weights=[w for _, w in WEIGHTS])[0]


def main():
    interval = 1.0 / RPS
    print(f"loadgen: {RPS} rps against {TARGET}", flush=True)
    while True:
        started = time.monotonic()
        with open(HEARTBEAT, "w", encoding="ascii") as fh:
            fh.write(str(time.time()))
        try:
            with urllib.request.urlopen(TARGET + pick_path(), timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError:
            pass  # 5xx responses are expected traffic
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            print(f"loadgen: request failed: {exc}", flush=True)
            time.sleep(1)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
