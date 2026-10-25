---
name: dodona-bringup
description: >-
  Bring up or debug one stage of the Dodona cluster — which gate has to pass before the
  next stage starts, what each stage's failure looks like, and the checks that are never
  skipped. Use when working through RUNBOOK.md, when a pod will not serve, when
  the gateway returns the wrong thing, or when asked what to do next on the box.
---

# Bringing up a stage

`RUNBOOK.md` is the source of truth. Each step has a gate. **The next step
does not start until the current gate passes** — not because of process, but because every
stage's failures are diagnosable only while the stage below it is known good.

## Before touching anything

Find out which stage is actually live. Do not trust the status table in `README.md`; it is
updated by hand and lags.

```
kubectl get pods -o wide                                     # fleet up?
curl -s localhost:8080/metrics | grep admitperf_gate          # admission on?
ls metrics/                                                   # any runs yet?
```

## The three gates that are never skipped

| Gate | Stage | Why it cannot be deferred |
|---|---|---|
| **`nvidia-smi` shows 3 processes on index 0, none above its slice** | 2 | If HAMi is not enforcing `gpumem`, one pod silently takes another's memory. The fleet is then not the fleet you documented, and **every number from every later stage is invalid with nothing to indicate it** |
| **A live `/metrics` scrape in Grafana** | 3 | One of the brief's two pass/fail items. Also: admission is a *function of* engine metrics, so the gateway has nothing to decide on without it |
| **A recorded hop, or a cold-vs-warm TTFT re-quote** | 8 | The other pass/fail item. `hop-or-warmup`, not both — but one of them must exist |

## Failure modes, by stage

Each of these looks like something else, which is why they are written down.

| Stage | Symptom | Actual cause |
|---|---|---|
| 0 | Pod loads, reports `READY`, refuses every request | Weights fit the budget but leave less than one sequence of KV. This is the Part 1 step-3 trap. Check `(gpumem x 0.90) - weights - activations >= kv_bytes_per_token x max_model_len` |
| 0 | Concurrency figures all wrong by a constant factor | `head_dim` derived as `hidden/heads` instead of read from `config.json`. **Always read it** |
| 2 | One pod has roughly half the KV tokens of its peers | Pods contended at startup. Restart them **one at a time** |
| 2 | Looks like a three-worker fleet, behaves like fewer | HAMi not enforcing the cap. The run is void |
| 3 | GPU utilisation panel says "No data" while everything else works | DCGM metrics never reach Prometheus. Cosmetic, but say so rather than leaving a blank panel in the report |
| 4 | Agent answers in prose, never calls a tool | The worker payload dropped `tools`. It is an allowlist — confirm `tools` and `tool_choice` cross, and that `kv_hop`/`prefix_hash`/`tenant` do not |
| 4 | Client-side TTFT suspiciously high and flat | The gateway synthesises SSE from a completed response, so client TTFT measures the whole generation. **TTFT comes from vLLM's metrics, always** |
| 6 | `admitperf report` prints an empty page | Three different causes, not interchangeable — see `dodona-run` |
| 6 | Every request admitted, signal flat at zero | A failed scrape returned `kv_cache_usage: 0.0`. A missing metric must be **omitted, never zeroed** — zero reads as an empty cache and makes every policy admit |
| 8 | `/hops` empty while requests succeed | `KV_BACKEND` is `nccl` or `nixl`. Both are stubs that return immediately. Check the backend **before** concluding anything about hops |
| 8 | TTFT implausibly good or bad on first requests | Replica not warm. `READY 1/1` precedes serving by minutes while CUDA graphs capture. Warm, then re-quote |

## What to do when a gate fails

1. **Do not advance.** A gateway bug debugged on top of a broken fleet costs hours.
2. **Log it in `SUBMISSION.md`'s deployment-issues table as it happens.** Reconstructing that
   table from memory at the end is how it ends up with three rows instead of twelve, and it
   is one of the more useful things in the submission.
3. If the fix changes a *number* — slice size, context, model — stop and run `dodona-capacity`
   before continuing. The arithmetic is load-bearing for every later stage.

## Cut order, when time runs out

From `RUNBOOK.md`, and do not reorder it in the moment: Experiment 2 whole, then
low-level seeds 2 and 3, then the Grafana exhibits, then the voice loop (text through the
gateway still proves the serve path).

**Never cut:** the fleet's integrity check, the Grafana scrape, the hop proof, or the
baselines. The first three are pass/fail; a run without a baseline has a one-sided
comparison.
