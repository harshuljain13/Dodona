# Results report — a deep research agent on 4 × A100-40GB

**Status: skeleton.** Every number is `TBM`. This is the document handed in; the
Part 0–8 files are the working notes behind it.

Measurement date: *TBM* · Commit: *TBM*

---

## Executive summary

*TBM — three paragraphs, written last.*

The shape it has to take, so it cannot be padded:

1. What was built, in two sentences.
2. **The liveness verdict.** Did `kv_cache_utilization` reach 0.90 on real agent
   traffic, and at which load level. This is the sentence the report exists for.
3. What the six predictions in [Part 1](01-capacity.md) got right and wrong.

---

## The artifact

The page printed at the end of every run, whether or not anyone remembers to ask
for it. Example below shows the **failing** case on purpose: a report that only
looks good when the policy fired would be no use, because the whole complaint is
that inert runs are currently invisible.

```
================================================================
AdmitPerf run report
run: kv-threshold-090-agent-high-a
cluster: 4x A100-40  ·  2 prefill + 2 decode  ·  disaggregated
model: Qwen2.5-32B-Instruct-AWQ int4  ·  vLLM 0.11.0  ·  commit TBM
----------------------------------------------------------------
SIGNAL LIVENESS: TBM
TBM

POLICY (5 axes)              |  SIGNAL LIVENESS
  Unit         request       |    signal: kv_cache_utilization
  Setting      online        |    threshold: 0.90
  Objective    deadline      |    min TBM    p50 TBM
  Signal       kv-pressure   |    p95 TBM    p99 TBM
  Portability  Class A       |    max TBM

SEVEN REPORTING ITEMS
  1 Capacity-relative load      TBM
  2 Calibrated deadlines        TBM
  3 Signal liveness             TBM
  4 Repeats and spread          TBM   3 seeds
  5 Parameter provenance        ok    threshold=0.90 (default)
  6 Metric definition           ok    denominator = offered
  7 Configuration disclosure    ok    config + sha in bundle
================================================================
```

Items 5, 6 and 7 are `ok` before the run because they are properties of the
tooling, not of the result. The other four are not knowable until there is a log.

**Why a report and not a dashboard.** The gap being closed is about what a write-up
hands a reader. A Grafana view that only exists while the cluster is up does not
close it. We have the dashboard too, because the lab supplies Grafana for free,
but it is a convenience rather than the point.

---

## Part 1 — The cluster as deployed

*TBM.* Design intent is [`02-cluster.md`](02-cluster.md); this section records
what actually came up, which is not always the same thing.

### Table 1 — Workers

| Pod | Role | GPU index | GPU mem used | KV tokens | 16k seqs |
|---|---|---|---|---|---|
| prefill-0 | prefill | *TBM* | *TBM* | *TBM* | *TBM* |
| prefill-1 | prefill | *TBM* | *TBM* | *TBM* | *TBM* |
| decode-0 | decode | *TBM* | *TBM* | *TBM* | *TBM* |
| decode-1 | decode | *TBM* | *TBM* | *TBM* | *TBM* |

**Four distinct GPU indices, or the run is void.** See
[`02-cluster.md`](02-cluster.md#the-check-that-has-to-pass-before-any-run-is-believed).
If the KV token counts differ between peers, pods contended at startup and the
pool-wide concurrency figure below is wrong.

### Table 2 — Everything else

| Component | Config | Notes |
|---|---|---|
| gateway | *TBM* | |
| Mooncake store | *TBM* | |
| Prometheus / Grafana | *TBM* | scrape interval *TBM* |
| AgentShip | laptop | adds round-trip to wall-clock, not to TTFT |

### Table 3 — Deployment issues

| Symptom | Cause | Fix |
|---|---|---|
| *TBM* | | |

Expected to be the longest table in the report. Candidates listed in
[`02-cluster.md`](02-cluster.md#deployment-issues).

---

## Part 2 — Capacity, predicted vs measured

Predictions from [`01-capacity.md`](01-capacity.md), not edited.

| | Predicted | Measured |
|---|---|---|
| `kv_bytes_per_token` | 256 KiB | *TBM* |
| Weights per card | 19.4 GiB | *TBM* |
| KV per card | 15.6 GiB | *TBM* |
| Concurrent seqs per worker @16k | 3.9 | *TBM* |
| Decode pool concurrency | 7.8 | *TBM* |
| Binding resource | KV | *TBM* |

---

## Part 3 — Benchmarks

Fixed trace, same across runs, via `cluster/app/locustfile.py`. This section
exists to characterise the fleet; Part 4 is the one that uses real traffic.

| Pattern | Shape | Purpose |
|---|---|---|
| `steady` | Poisson, well under ceiling | baseline latency on a non-saturated fleet |
| `burst` | all at once | does the admission gate absorb it, or does vLLM queue |
| `multiturn` | sessions sharing the 1k prefix | what prefix caching is worth here |

### Table 4 — Results

| | steady | burst | multiturn |
|---|---|---|---|
| Output tok/s | *TBM* | *TBM* | *TBM* |
| TTFT p50 / p95 / p99 | *TBM* | *TBM* | *TBM* |
| Inter-token mean / p95 | *TBM* | *TBM* | *TBM* |
| Prefix-cache hit rate | *TBM* | *TBM* | *TBM* |
| `kv_cache_usage` max | *TBM* | *TBM* | *TBM* |
| `num_requests_waiting` max | *TBM* | *TBM* | *TBM* |
| Preemptions | *TBM* | *TBM* | *TBM* |

---

## Part 4 — Agent validation, and the liveness measurement

The deliverable. Real AgentShip traffic, not a synthetic trace.

### Table 5 — Runs

| | low | low-baseline | high | high-baseline |
|---|---|---|---|---|
| Policy | `kv_threshold` 0.90 | `no_admission` | `kv_threshold` 0.90 | `no_admission` |
| Concurrent sessions | 2 | 2 | 8 | 8 |
| Questions | *TBM* | *TBM* | *TBM* | *TBM* |
| **`kv_pressure` max** | *TBM* | *TBM* | *TBM* | *TBM* |
| **Liveness verdict** | *TBM* | n/a | *TBM* | n/a |
| Admitted / deferred / rejected | *TBM* | *TBM* | *TBM* | *TBM* |
| Prompt tok/s offered | *TBM* | *TBM* | *TBM* | *TBM* |
| Prefix-cache hit rate | *TBM* | *TBM* | *TBM* | *TBM* |
| vLLM mean TTFT | *TBM* | *TBM* | *TBM* | *TBM* |
| `num_requests_waiting` max | *TBM* | *TBM* | *TBM* | *TBM* |
| Preemptions | *TBM* | *TBM* | *TBM* | *TBM* |
| Wall time p50 / p95 | *TBM* | *TBM* | *TBM* | *TBM* |

The two bold rows are the result. Everything else is context that makes them
believable.

### Table 6 — Workload, measured

*TBM.* Fills in [`08-proof.md`](08-proof.md#workload-measurements).

### Table 7 — Predictions

*TBM.* Fills in [`08-proof.md`](08-proof.md#checking-the-predictions).

### Cluster behaviour

*TBM.* What the agent traffic looked like from the fleet's side: prefill/decode
ratio, cacheability, where the queue sat.

### Attribution caveats

*TBM.* Start from the list in
[`08-proof.md`](08-proof.md#honest-attribution-notes) and add whatever the run
actually compromised on.

---

## Conclusions and next steps

*TBM.*

---

## Exhibits

Grafana, 30-minute windows, one pair per load level.

| | |
|---|---|
| A | latency and throughput |
| B | KV cache and scheduling |
| C | router and GPU |
| D | admission queue and decisions |

---

## Appendix — reproduction

| | |
|---|---|
| Bring the fleet up | *TBM* |
| Run the benchmarks | *TBM* |
| Run the agent | *TBM* |
| Generate a report | `admitperf report results/<exp>/<run>/decisions.jsonl` |
| Compare two runs | `admitperf compare <baseline> <policy>` |

Bundles under [`../results/`](../../infra/metrics/README.md).
