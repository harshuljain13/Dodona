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
make fleet-runpod-status                      # which workers, which cards, how full, one fleet?
make fleet-runpod-smoke-test                  # does a request come back — with tools?
curl -s localhost:8080/metrics | grep admitperf_gate_active   # policy=none means the gate is OFF
ls metrics/                                   # any runs yet?
```

`status` before `smoke-test`: the smoke test tells you whether the fleet works,
`status` tells you whether it is the fleet you think. Both have been wrong while
every port was listening.

## The three gates that are never skipped

| Gate | Stage | Why it cannot be deferred |
|---|---|---|
| **`nvidia-smi` shows one process per card, and `make fleet-runpod-status` says `fleets: 1`** | 2 | Two fleets on one box each take half the memory and neither is the fleet you documented. Count TOP-LEVEL `workers.sh` processes, not all of them: one healthy fleet is a parent plus one supervisor per component, which was ten the day the check said `> 7` meant two fleets |
| **A request carrying `tools` comes back with `choices`** | 3 | vLLM 400s every tool request unless started with `--enable-auto-tool-choice --tool-call-parser`, and the gateway reports that as **200 with no `choices`** — litellm then asserts, the agent apologises aloud, and nothing logs a cause at any layer. A plain completion passes while the agent cannot finish a single real turn |
| **A live `/metrics` scrape in Grafana** | 3 | Admission is a *function of* engine metrics, so the gateway has nothing to decide on without it |
| **`kv_cache_usage_perc` reaching the policy's threshold under load** | 8 | Otherwise the run is **INERT**: the policy was never asked a hard question, so its numbers are indistinguishable from having no policy. Check it BEFORE generating load — `benchmarking/04-routing-admission` does |

## Failure modes, by stage

Each of these looks like something else, which is why they are written down.

| Stage | Symptom | Actual cause |
|---|---|---|
| 0 | Pod loads, reports `READY`, refuses every request | Weights fit the budget but leave less than one sequence of KV. This is the Part 1 step-3 trap. Check `(gpumem x 0.90) - weights - activations >= kv_bytes_per_token x max_model_len` |
| 0 | Concurrency figures all wrong by a constant factor | `head_dim` derived as `hidden/heads` instead of read from `config.json`. **Always read it** |
| 2 | One pod has roughly half the KV tokens of its peers | Pods contended at startup. Restart them **one at a time** |
| 2 | `fleet-runpod` printed a full green bring-up and nothing changed | A fleet was already running: `workers.sh` aborted on `check_ports` because the old fleet held them, while the readiness poll got its answers from that same old fleet. Fixed — it stops the running fleet first — but the shape recurs whenever a check and its subject share a signal |
| 2 | Workers never come ready, poll runs to its timeout | The poll was waiting on a port this topology does not use. It reads the worker list from `layout.sh` on the box now; three scripts share that file for exactly this reason |
| 3 | GPU utilisation panel says "No data" while everything else works | DCGM metrics never reach Prometheus. Cosmetic, but say so rather than leaving a blank panel in the report |
| 4 | Agent answers in prose, never calls a tool | The worker payload dropped `tools`. It is an allowlist — confirm `tools` and `tool_choice` cross, and that `kv_hop`/`prefix_hash`/`tenant` do not |
| 4 | Client-side TTFT suspiciously high and flat | The gateway synthesises SSE from a completed response, so client TTFT measures the whole generation. **TTFT comes from vLLM's metrics, always** |
| 6 | `admitperf report` prints an empty page | Three different causes, not interchangeable — see `dodona-run` |
| 6 | Every request admitted, signal flat at zero | A failed scrape returned `kv_cache_usage: 0.0`. A missing metric must be **omitted, never zeroed** — zero reads as an empty cache and makes every policy admit |
| 4 | Agent says "let me look that up", then answers from memory | `streaming` was left unset. This gateway buffers the whole completion and re-emits one chunk built from the assistant text alone — `tool_calls` are not copied into the delta, so the call is dropped with no error. `streaming: false` in the agent spec |
| 6 | Every request refused with 429 under load | The per-tenant quota, not capacity. `TENANT_TOKENS_PER_MIN` defaults to 10,000, which one research turn exceeds — and a 429 in a report is indistinguishable from the policy working unless you separate them |
| 6 | 400 `prompt_too_long` partway through a research turn | `MAX_PROMPT_CHARS`. A scraped page becomes part of the next model call, so the second hop exceeds a cap the engine's own context would have allowed |
| 8 | The ledger shows zero hops | Expected: one pool means `queue.py` short-circuits and no hop is attempted. On a split fleet it means no connector — `KVBus.transfer` writes a row, `nccl.py` and `nixl.py` are `return`. Neither moves bytes |
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
