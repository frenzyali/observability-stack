# Runbooks

One section per alert. Every alert's `runbook_url` annotation links here. Each runbook says what the alert means, how to confirm it, and what to do. Commands assume the repo root and the `obs-stack` Compose project.

Useful everywhere:

```bash
docker compose -p obs-stack ps                      # service status and health
docker compose -p obs-stack logs --tail 100 <svc>   # recent logs
open http://127.0.0.1:9090/alerts                   # what Prometheus thinks is firing
open http://127.0.0.1:9093                          # what Alertmanager is notifying about
```

## InstanceDown

**Meaning:** Prometheus could not scrape a target for 1 minute. Either the process is down, or Prometheus cannot reach it. Severity `critical`. Alertmanager inhibits every other alert with the same `instance` while this fires.

**Check**
1. Prometheus → Status → Targets: read the `lastError` for the target (connection refused = process down; timeout = overloaded or network).
2. `docker compose -p obs-stack ps <service>`: is the container running and healthy?
3. `docker compose -p obs-stack logs --tail 100 <service>`: crash or OOM-kill on exit?

**Fix**
- Container stopped or crashed: `docker compose -p obs-stack up -d <service>`. If it keeps dying, see [ContainerRestarting](#containerrestarting).
- Running but unscrapeable: check the port and `/metrics` path from inside the network: `docker compose -p obs-stack exec prometheus wget -qO- http://<target>/metrics | head`.

## ScrapeStale

**Meaning:** Prometheus has no successful scrape of a target in the last 3 scrape intervals (45s), confirmed for one more evaluation. Either the last 3 scrapes all failed, or Prometheus stopped writing samples for the target at all, so `up` never turns 0 and [InstanceDown](#instancedown) stays quiet. Severity `warning`. Every `rate()`-based alert on the target (error rate, latency, SLO burn) is working from old data and can fire or resolve for the wrong reason. If `InstanceDown` also fires for the same `instance`, it inhibits this alert: follow that runbook instead.

**Check**
1. Prometheus → Status → Targets: compare *Last scrape* with the interval, and read `lastError`.
2. Stalled scrape loop: is Prometheus itself struggling? *Monitoring self-health* dashboard (scrape duration, rule evaluation time, memory); `docker compose -p obs-stack logs --tail 100 prometheus`.
3. Slow target: a `scrape_duration_seconds` close to the 10s `scrape_timeout` means scrapes are timing out.

**Fix**
- Target failing: as for [InstanceDown](#instancedown).
- Prometheus overloaded or stuck: give it more CPU/memory, cut cardinality (`metric_relabel_configs`), or restart it as a stop-gap: `docker compose -p obs-stack restart prometheus`.
- Until data is fresh again, treat the target's other alerts (firing *or* resolved) as unreliable.

## HostHighCPU

**Meaning:** Host CPU has been more than 90% busy (all cores, 5m rate) for 10 minutes. Severity `warning`. This is a cause, not a user-facing symptom, so it opens a ticket instead of paging.

**Check**
1. *Node overview* dashboard → *CPU by mode*. High `user`/`system` means real work; high `iowait` points at disk; high `steal` points at a noisy neighbour on a VM.
2. *Container overview* → *CPU by container* to find the consumer; map the ID to a name with `docker ps --no-trunc | grep <id>`.

**Fix**
- Throttle or limit the offending container (`deploy.resources.limits.cpus`).
- If load is legitimate and sustained, add capacity.

## HostHighMemory

**Meaning:** Less than 10% of host memory is available (`1 - MemAvailable/MemTotal > 0.9`) for 10 minutes. Severity `warning`. The OOM killer is close.

**Check**
1. *Node overview* → *Memory utilisation*: sudden jump (new workload, leak) or slow climb (leak)?
2. *Container overview* → *Memory working set by container* for the largest consumers.

**Fix**
- Restart a leaking container as a stop-gap and file a bug.
- Set or lower `deploy.resources.limits.memory` so a leak kills one container, not the host.

## HostDiskSpaceLow

**Meaning:** A real filesystem (tmpfs/overlay excluded) has less than 10% free space for 5 minutes. Severity `critical`: full disks break databases, logs and Docker itself.

**Check**
1. Which mountpoint? It is in the alert labels and on *Node overview* → *Filesystem space used*.
2. Find the space: `df -h <mountpoint>`, then `du -xh --max-depth=1 <mountpoint> | sort -h | tail`.
3. Docker usage: `docker system df`.

**Fix**
- Delete or rotate large logs; prune *your own* unused images or build cache deliberately (review before deleting; do not blanket-prune shared hosts).
- Prometheus data itself: lower `--storage.tsdb.retention.time` or add `--storage.tsdb.retention.size`.

## HostDiskWillFillIn24h

**Meaning:** Linear extrapolation of the last 6h of free space reaches zero within 24 hours, and less than 40% is left. Severity `warning`. Gives you a day of warning before [HostDiskSpaceLow](#hostdiskspacelow).

**Check**
1. *Node overview* → *Free space, 24h linear prediction*: steady growth or a one-off burst? A single large copy can trigger a false prediction that clears by itself.
2. Find what is growing (same commands as HostDiskSpaceLow), compared over time.

**Fix**
- Stop the growth (log level, runaway job), add rotation, or grow the volume.

## ContainerRestarting

**Meaning:** A container has started more than 2 times in 15 minutes: a crash loop. Severity `warning`.

**Check**
1. Map the short ID to a name: `docker ps -a --no-trunc | grep <container_id>`.
2. Why did it exit? `docker inspect <name> --format '{{.State.ExitCode}} OOMKilled={{.State.OOMKilled}}'` and `docker logs --tail 100 <name>`.

**Fix**
- `OOMKilled=true`: raise the memory limit or fix the leak (see [ContainerHighMemory](#containerhighmemory)).
- Exit on startup: usually bad config or a missing dependency. Fix it and redeploy. A restart policy cannot fix a config error.

## ContainerHighMemory

**Meaning:** A container's working set has been above 90% of its memory limit for 5 minutes. Severity `warning`. At 100% the kernel OOM-kills it. Containers without a limit are excluded.

**Check**
1. *Container overview* → *Memory used vs limit*: steady climb (leak) or plateau (limit too low)?

**Fix**
- Plateau under normal load: raise `deploy.resources.limits.memory` in `compose.yaml`.
- Climb: restart as a stop-gap and fix the leak.

## ProbeFailed

**Meaning:** blackbox-exporter has not had a 2xx from an HTTP endpoint for 2 minutes. This is what a user would see. Severity `critical`.

**Check**
1. *Blackbox uptime* dashboard: which target, which status code (*Last HTTP status code*), and in which phase is the time spent?
2. Reproduce from inside the network: `docker compose -p obs-stack exec prometheus wget -S -qO- <target-url>`.

**Fix**
- Treat it like the service being down for users: restart or roll back the service, then find the cause in its logs.
- If only the probe is broken (wrong URL or module), fix `prometheus/prometheus.yml` or `blackbox/blackbox.yml`.

## HighErrorRate

**Meaning:** More than 5% of sample-app requests returned 5xx over the last 5 minutes, for 2 minutes, with at least 0.1 req/s of traffic. Severity `critical`.

**Check**
1. *Sample app: RED and SLO* → *Requests by status* and *Requests by route*: one route or all of them?
2. App logs: `docker compose -p obs-stack logs --tail 100 sample-app`.
3. Is fault injection on? `curl -s http://127.0.0.1:8000/chaos` (this demo's chaos API).

**Fix**
- Roll back the last change if errors started with a deploy.
- Clear injected faults: `scripts/chaos.sh clear`.
- Dependency failing: fail fast and shed load instead of queueing.

## HighLatencyP95

**Meaning:** The 95th-percentile request latency of sample-app has been above 1s for 5 minutes. Severity `warning`.

**Check**
1. *Sample app: RED and SLO* → *p95 latency by route*: one slow route (e.g. `/slow`) or everything?
2. Container CPU throttling or host CPU saturation ([HostHighCPU](#hosthighcpu)).
3. Injected latency: `curl -s http://127.0.0.1:8000/chaos`.

**Fix**
- Everything slow: add capacity (more gunicorn threads or workers), check limits.
- One route: profile that handler.
- Clear injected latency: `scripts/chaos.sh clear`.

## SLOErrorBudgetBurnFast

**Meaning:** sample-app is spending its error budget fast. Either >14.4× the sustainable rate over both 1h and 5m, or >6× over both 6h and 30m. At 14.4× the whole 7-day budget is gone in about 12 hours. Severity `critical` (page).

**Check**
1. *Sample app: RED and SLO* → *Error budget burn rate by window* and *Error budget remaining*.
2. Then debug as for [HighErrorRate](#higherrorrate). The burn-rate alert tells you the impact; the error-rate panels tell you where.

**Fix**
- Stop the bleeding first (rollback, clear faults), then investigate.
- After the incident: if the budget is exhausted, prioritise reliability work over features until it recovers.

## SLOErrorBudgetBurnSlow

**Meaning:** A slower but sustained burn: >3× over 1d and 2h, or >1× over 3d and 6h. Not urgent, but the budget will run out before the 7-day window ends if nothing changes. Severity `warning` (ticket). Inhibited while the fast-burn alert fires.

**Check**
1. *Error budget burn rate by window*: a constant trickle of errors, often one route or one client.

**Fix**
- Find and fix the persistent error source during working hours.
- If the SLO is unrealistic for this service, change it on purpose and record why.
