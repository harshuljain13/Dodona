# Part 8 — Proof from app traffic

**Status: stub.** This is where the run lands. The deliverable is the report
page, generated from agent traffic rather than from a synthetic trace.

## The two runs

One policy, `kv_threshold` at 0.90, on real AgentShip traffic. Plus the
`no_admission` baseline at each level, so every comparison has two sides.

| Run | Concurrent sessions | Why that number |
|---|---|---|
| `low` | 2 | ~50% of the 7.8-sequence decode pool. Pressure should stay well under 0.90 |
| `high` | 8 | Comfortably past the pool. Pressure should reach 0.90 |
| `low-baseline` | 2 | `no_admission`, same trace |
| `high-baseline` | 8 | `no_admission`, same trace |

Three seeds each, because reporting item 4 is repeats and spread, and a single
run cannot distinguish a threshold crossing from noise.

## Experiment 1 — Signal liveness

Does `kv_cache_utilization` reach 0.90?

Three outcomes, all reportable:

| Outcome | What it means |
|---|---|
| Reaches it at high load, not at low | **The transition point is measured** — we know where this fleet's door starts closing |
| Reaches it at both | The policy fired throughout; report the range and the refusals |
| Reaches it at neither | The policy was **inert** on this hardware with this workload. Still a result, and the report says `FAILED` on line one rather than burying it |

The inert case is the one to be honest about in advance. It reads like a null
result to anyone who does not read the report, which is why the verdict is the
first line and why the presentation leads with it rather than apologising for it.

Given [Part 1](01-capacity.md)'s arithmetic — 7.8 concurrent sequences in the
decode pool against 8 concurrent sessions — the first outcome is predicted. If it
does not happen, the first thing to check is whether all four GPUs were actually
in use ([Part 2](02-cluster.md#the-check-that-has-to-pass-before-any-run-is-believed)).

## Experiment 2 — Does the shared prefix change the answer

**Only if there is time.** Same policy, two traffic shapes: one where the agent
reuses its system prompt and tool schemas across sessions (the normal case), one
where the prefix differs every time (worst case for the cache).

If the signal range moves between them, the traffic mix matters as much as the
hardware for whether admission fires. If it does not, the signal is a property of
the fleet rather than of the workload — and we say which.

Better dropped than half-done.

## Checking the predictions

Copied from [Part 1](01-capacity.md#predictions-written-before-the-run) and
filled in after. Not edited to match.

| # | Prediction | Measured | Held? |
|---|---|---|---|
| P1 | KV binds, not the scheduler | *TBM* | *TBM* |
| P2 | Signal reaches 0.90 at high load | *TBM* | *TBM* |
| P3 | Signal does not reach 0.90 at low load | *TBM* | *TBM* |
| P4 | Prefix-cache hit rate > 70% | *TBM* | *TBM* |
| P5 | Prompt:completion ratio > 10:1 | *TBM* | *TBM* |
| P6 | `kv_bytes_per_token` = 256 KiB ±5% | *TBM* | *TBM* |

P6 comes first in time: it is read off `config.json` on a live worker, and if it
is wrong every concurrency figure in Part 1 moves with it.

## Workload measurements

[Part 0](00-use-case.md)'s token counts were estimates. Measured from a real
session:

| | Predicted | Measured |
|---|---|---|
| System prompt + tool schemas | ~1k | *TBM* |
| Fetched source, after truncation | ~12k | *TBM* |
| Longest request observed | — | *TBM* |
| Mean prompt tokens | — | *TBM* |
| Mean completion tokens | — | *TBM* |
| Turns per question | ~4 | *TBM* |
| Tool calls per question | — | *TBM* |

## What the bundle contains

One directory per run under [`../results/`](../../infra/metrics/README.md):

```
results/<experiment>/<run>/
  meta.json            config, commit sha, model, vLLM version, seed
  decisions.jsonl      the decision log — one row per admission decision
  report.txt           admitperf report decisions.jsonl
  metrics/             /metrics snapshots, per worker, before and after
  grafana/             screenshots: latency, KV, router, admission queue
```

Produced by `admitperf report` and `admitperf compare`. The report is the
artifact; the rest is what makes it checkable.

## Honest-attribution notes

Written here in advance because these are the caveats that get forgotten once
numbers exist.

- **Two runs an hour apart are not a controlled comparison.** Prefix caches warm,
  weights stay resident, and a fixed seed means prompt reuse across runs. Runs are
  back to back, same deploy, and the seed changes between repeats.
- **The fleet must not change size between runs.** A fleet that resizes means the
  deployment moved, not just the policy.
- **TTFT comes from vLLM, not the client.** The gateway synthesises SSE from a
  completed response, so client-side TTFT measures the whole generation. See
  [Part 7](07-wiring.md#the-fix).
- **Wall-clock per question includes laptop-to-Lambda round trips**, because the
  agent runs on the laptop. It does not affect TTFT or KV pressure.
- **Overflow budget exhaustion looks like overflow not helping.** The run summary
  must say when the budget ran out.
