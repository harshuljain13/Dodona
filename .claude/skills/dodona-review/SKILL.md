---
name: dodona-review
description: >-
  Adversarial review for Dodona changes, tuned to the defects this project actually
  produces — numbers that no longer match the hardware, and claims no scrape supports. Use
  when reviewing a diff or a PR, or before marking anything done.
---

# Reviewing a change

This project has two characteristic ways of being wrong. Everything below is aimed at one
of them.

> **A number that no longer matches the hardware.**
> **A claim that no scrape supports.**

## Pass 1 — numbers

| Check | Why |
|---|---|
| Does any number in the diff disagree with `docs/project/01-capacity.md`? | That file is the authority. A disagreement is a bug in one of the two, and it is usually not the authority |
| Was `gpu_memory_utilization` applied to the **slice** or the card? | HAMi gives the pod a hard `gpumem` cap. Against the card instead of the slice is a 3× error here |
| Was `head_dim` read from `config.json` or derived? | `hidden/heads` is wrong for Qwen3 by 60% |
| Do quantized weights account for the **unquantized** embeddings and `lm_head`? | 2.03 GiB on Qwen2.5-7B — 39% of the footprint |
| Are **both** lengths reported — `max_len` and the real send length? | The brief asks for both. They differ 16× here |
| Did a prediction get **edited** to match a measurement? | Never allowed. Record what was predicted and what happened, side by side |

## Pass 2 — claims

For every assertion in the diff, ask **what artifact backs this?**

| Claim shape | Required artifact |
|---|---|
| "concurrency is N" | the arithmetic, or a scrape |
| "the policy fired" | `decisions.jsonl` rows with the signal value, plus a liveness verdict |
| "the hop works" | `/hops` non-empty **and** `KV_BACKEND=mooncake` confirmed |
| "TTFT is X" | vLLM's metrics, from a **warm** replica |
| "throughput improved" | token counts alongside the wall time |
| "three workers" | `nvidia-smi` showing three processes, none above its slice |

A claim with no artifact is not a finding. Either get the artifact or mark it `TBM`.

## Pass 3 — the specific traps

These have each produced a wrong result at least once, in this project or the lab it came
from.

| | |
|---|---|
| **A zeroed metric** | `kv_cache_usage: 0.0` reads as an empty cache and makes every policy admit. Missing values must be **omitted**, never defaulted |
| **Averaged pressure** | Pressure comes from the replica with the most headroom, because that is where the router places. An average lets one idle replica hide a saturated fleet |
| **A gate that decides but does not record** | Produces an empty report that reads exactly like "never fired". `admitperf_gate_recording` is a separate metric for this reason |
| **A policy that fails closed** | Sheds all traffic on a bug. Fail open — and keep `admitperf_gate_active` separate, or a crashing policy is indistinguishable from `no_admission` |
| **A 429 that overflowed** | A quota refusal is client-attributable. Paying a provider for it is overspending, not a capacity event. On the brief's bad-answer list |
| **A stub transport** | `nccl.py` and `nixl.py` return immediately. Any hop claim must name the backend |
| **A dropped field** | The worker payload is an allowlist. If `tools` stops crossing, the agent answers in prose and the workload silently becomes single-turn chat |
| **A re-pick after a shed** | Do not bounce. One refusal becomes three attempts' latency and the shed-by-reason panel stops meaning anything |

## Pass 4 — scope and honesty

- Does a status table now over-claim? Check it against the tree, not against the diff.
- Does `docs/README.md` still correctly list which deep dives are stale?
- If the change bounded coverage — fewer seeds, one load level, a skipped stage — **is that
  said out loud?** Silent truncation reads as "covered everything".
- Is an inert or negative result being softened? Lead with it instead.

## What not to flag

- Prose in `docs/cluster/*` describing code that still behaves that way. Those docs
  intentionally document the stack, not this project's choices.
- A `TBM`. That is the convention working.
- A prediction that looks wrong. Predictions are allowed to be wrong; that is their job.
