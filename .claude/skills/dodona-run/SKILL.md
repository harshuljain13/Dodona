---
name: dodona-run
description: >-
  Run a Dodona experiment and read what it produced — the saturation check that comes
  before any comparison, what a run bundle contains, and the one question that decides
  whether a result means anything. Use when running a benchmark or load test, reading
  results, comparing a policy against its baseline, or interpreting a report.
---

# Running an experiment, and reading it

## The question that comes first

> **Was anything actually scarce during this run?**

If `kv_cache_usage_perc` never approached 0.90, the policy was **inert**: it ran on every
request and could not have changed a single outcome. A cluster in that state produces
latency and throughput numbers that look completely normal — so every downstream comparison
is measuring something other than the policy.

Check this **before** reading any other number. `admitperf report` puts the verdict on line
one for exactly this reason.

## The four runs

Never fewer. A policy run without its baseline is a one-sided comparison.

| Run | Policy | Sessions |
|---|---|---|
| `low-baseline` | `no_admission` | 2 |
| `low` | `kv_threshold` 0.90 | 2 |
| `high-baseline` | `no_admission` | 14 |
| `high` | `kv_threshold` 0.90 | 14 |

Three seeds each, back to back, same deploy, seed varied between repeats.

**The baseline is `no_admission`, not the gate switched off.** With the gate off nothing is
logged, so there is no baseline report to compare against — the comparison would have one
side. `no_admission` admits everything and records the metrics behind each decision, which
gives the baseline the same shape of log.

**The fleet must not change size between runs in a comparison.** A resized fleet means the
deployment moved, not the policy.

## A bundle

```
metrics/<run>/
  meta.json          config, commit sha, model, vLLM version, seed
  decisions.jsonl    one row per admission decision, with the signal value read
  report.txt         admitperf report decisions.jsonl
  pods/              /metrics per pod, before and after
  hops.json          /hops — count, bytes, failed writes, evictions
```

```
admitperf report  metrics/high-a/decisions.jsonl
admitperf compare metrics/high-baseline-a metrics/high-a
```

## Read it in this order

1. **`meta.json`** — is this the fleet you think it is? Three pods, right model, right commit.
2. **The liveness verdict**, line one of `report.txt`. If `FAILED`, nothing below it is about the policy.
3. **The signal range** — min / p50 / p95 / p99 / max.
4. Only then latency and throughput.

Going in the other order is how an inert run gets read as a working policy.

## An empty bundle means three different things

| | |
|---|---|
| No `decisions.jsonl` | the gate never ran. Check `admitperf_gate_active` |
| Empty file, gate active | the log was unwritable. Check `admitperf_gate_recording` |
| Rows present, last few missing | the run ended without a flush — and **the lost rows are the saturation event** |

None of these is "the policy never fired." That is a fourth thing, and only the report can
tell you it happened.

## Numbers that are wrong in a way that is hard to see

| | |
|---|---|
| **TTFT from the client** | The gateway synthesises SSE from a completed response, so client-side TTFT measures the whole generation. Take it from vLLM's metrics |
| **TTFT from a cold replica** | `READY` precedes serving by minutes. Warm, then re-quote. On the brief's bad-answer list |
| **Hops when `KV_BACKEND` is not mooncake** | `nccl.py` and `nixl.py` return immediately. An empty `/hops` panel under those means nothing at all |
| **Overflowed responses pooled with local ones** | Overflow is a 4B answering for a 7B — a relief valve, not equivalent service. Never pool them for quality |
| **Overflow "not helping"** | `OVERFLOW_MAX_REQS=20` is a **lifetime** cap, not a rate. After 20 the path switches itself off. The summary must say when the budget ran out |
| **Wall-clock including the laptop** | The agent runs on the laptop, so round-trips inflate wall time. They do not affect TTFT or KV pressure |
| **Preemptions > 0** | Not noise. It means the gate admitted more than the fleet could hold, and the engine did admission control after the fact |

## Reporting an inert result

Lead with it. It is a result: the policy was installed, it ran, and this hardware with this
workload never gave it anything to fire on. Say which load level, say the range the signal
took, and say what would have had to change.

Burying it is the failure mode — not having it.
