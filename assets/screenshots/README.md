# assets/screenshots/

Grafana exhibits and generated charts. Everything here comes from a run in
[`infra/metrics/`](../../infra/metrics/) and carries the run id, so it can be
regenerated rather than trusted.

## The eight dashboards

The brief asks us to walk through these in the presentation.

| # | Dashboard | Shows |
|---|---|---|
| 1 | Cluster | three pods on one GPU, slice enforcement |
| 2 | Success and failures | 200s against 400/429/503/529 |
| 3 | Gateway + admission | **sheds by reason** — `tenant_tokens`, `timeout_queue`, `kv_free` |
| 4 | Router | placement by pod, prefix-match rate |
| 5 | Queue depth by pod | `orch_replica_queue_depth` |
| 6 | vLLM | KV usage, waiting/running, preemptions, prefix hits |
| 7 | Mooncake KV | `/hops`, bytes moved, failed writes, evictions |
| 8 | Pods / replicas | per-slice memory against the `gpumem` cap |

Captured as a pair per load level — low and high — over the same window width, so
the two are comparable at a glance.

## Charts

| | |
|---|---|
| `kv-pressure-vs-load.png` | `kv_cache_usage_perc` against concurrent sessions, with 0.90 as a dashed line. **The headline chart** — it is the liveness verdict in one picture |
| `ttfa-vs-full-answer.png` | the two voice SLOs on one axis, p50 and p95. The only chart a listener would recognise |
| `shed-by-reason.png` | refusals over a run, stacked by reason |
| `queue-depth-by-pod.png` | `num_requests_waiting` per pod, shared-prefix mix against unique-prefix |
| `hop-vs-recompute.png` | measured transfer time against measured recompute for a 12k prompt — settles prediction P8 |

**Status: none yet.** Nothing has run on hardware.
