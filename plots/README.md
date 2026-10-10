# plots/

Charts generated from [`../metrics/`](../metrics/), never hand-drawn.

| | |
|---|---|
| `kv-pressure-vs-load.png` | `kv_cache_usage_perc` against concurrent sessions, with the 0.90 threshold as a dashed line. **The headline chart** |
| `shed-by-reason.png` | 400 / 429 / 503 / 529 over a run |
| `queue-depth-by-pod.png` | `num_requests_waiting` per pod, two traffic mixes |
| `prefix-cache-hit.png` | hit rate on the shared-prefix mix |
| `ttfa-vs-full-answer.png` | the two voice SLOs on one axis, p50 and p95 |
| `hop-vs-recompute.png` | measured transfer time against measured recompute |

**Status: none yet.** Every plot carries the run id it came from in its caption,
so it can be regenerated.
