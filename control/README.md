# control/ — the control plane

The six decisions the brief says we must own. This is the box that decides what
reaches a GPU.

| Decision | Signature | Part |
|---|---|---|
| **guard** | `inspect(payload) → Guard` | [3](../SUBMISSION.md#part-3--guardrails-admit-stay-vs-leave) |
| **admit** | `should_shed(req, snap) → (shed?, code, reason, retry_after_seconds)` | [3](../SUBMISSION.md#part-3--guardrails-admit-stay-vs-leave) |
| **place** | `pick(req, workers, *, policy) → Worker \| Shed` | [4](../SUBMISSION.md#part-4--place) |
| **queue / hop** | `transfer(src, dst)` | [5](../SUBMISSION.md#part-5--queue), [6](../SUBMISSION.md#part-6--hop-when-do-you-move-kv-is-the-replica-warm) |
| **declare warm** | warmup gate, then re-quote | [6](../SUBMISSION.md#part-6--hop-when-do-you-move-kv-is-the-replica-warm) |
| **stay vs leave** | overflow gate | [3](../SUBMISSION.md#part-3--guardrails-admit-stay-vs-leave) |

**Status: not written.** Stages 4–8 of
[`../RUNBOOK.md`](../RUNBOOK.md), built one at a time.

## What is *not* here

vLLM owns the waiting queue, the block table, preemption and the kernels. We set
engine flags and leave the scheduler alone — "I reimplemented vLLM's scheduler"
is on the brief's bad-answer list.

**We hold no queue of our own.** Requests block on a thread; the real queue is
inside the engine. That is why `queue_depth` is scraped from `/metrics` rather
than read out of a local data structure.

## Admission is a socket

The policy comes from [AdmitPerf](https://github.com/harshuljain13/AdmitPerf)'s
registry, selected by environment variable with no code change:

```
ADMISSION_POLICY      kv_threshold | queue_depth | dual_gate | no_admission
ADMISSION_THRESHOLD   0.90
ADMISSION_LOG         ../metrics/<run>/decisions.jsonl
```

**The baseline is `no_admission`, not the gate switched off.** With the gate off
nothing is logged, so the comparison would have one side.

Three invariants, each of which prevents a report that looks fine and is wrong:

- **A missing metric is omitted, never zeroed.** `kv_cache_usage: 0.0` reads as a
  completely empty cache — the one value that guarantees every policy admits, so
  a failed scrape would silently switch admission off.
- **Pressure comes from the replica with the most headroom**, not the average. An
  average lets one idle replica hide a saturated fleet.
- **The policy fails open**, with `admitperf_gate_active` as a separate metric, so
  a crashing policy is distinguishable from `no_admission` in the outcome.

## The tool pass-through

The worker payload is an **allowlist**, not the whole inbound body: our control
keys (`kv_hop`, `prefix_hash`, `tenant`, `priority`) mean nothing to the engine
and an unknown key is a 400. But `tools` and `tool_choice` **must** cross, or the
agent gets no schemas, answers in prose, and its loop ends after one turn — which
looks like a weak model rather than a dropped field.
