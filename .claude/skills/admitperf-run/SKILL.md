---
name: admitperf-run
description: >-
  Run an AdmitPerf experiment and read what it produced — the saturation check that comes
  before any comparison, what a bundle contains, and the one question that decides whether
  a result means anything. Use when running a benchmark, reading results, comparing
  policies, or interpreting a report.
---

# Running an experiment, and reading one

## Ask this first, every time

**Did the signal move?**

A policy that never reached its threshold did not perform badly. It never ran, and it
reports the same numbers as having no policy at all. Until you know the signal's observed
range, a results table tells you nothing.

This is not hypothetical. `experiments/half-capacity-headroom` recorded `waiting_requests`
identically **zero** across all three repeats, with `running_requests` peaking at 6-8
against a cap of 10. The engine was never saturated, so *every* signal was flat — not just
the one under test. The run was treated as banked evidence for weeks. It isolates nothing.

## Before comparing anything

1. **Establish capacity.** `bench capacity` finds what the deployment can actually serve.
   Comparing policies below that ceiling compares nothing.
2. **Check the load is capacity-relative.** "15 rps" is meaningless alone; "15 rps at 46%
   of ceiling" is a number someone else can reproduce.
3. **Confirm the engine queues internally.** Load must back up *inside* vLLM, not in front
   of it. If `max_concurrent_inputs` is below the engine's `max_num_seqs`, requests wait at
   the platform and the admission signal stays flat while latency climbs.

## What a bundle contains

```
results/<timestamp>-<experiment>/<policy>-r<n>/
├── manifest.json     what ran, against what, with which settings and commit
├── summary.json      the numbers, each tagged with where it came from
├── decisions.jsonl   every admit/defer/reject, with the state it was decided on
└── outcomes.jsonl    per-request TTFT, inter-token gaps, deadline verdict
```

`decisions.jsonl` is the one that matters. A verdict alone cannot be interpreted after the
fact; the state beside it can.

## Reading a result

- **Identical numbers across policies** is a finding, not a failure — but only once you can
  say whether it is because the policies agreed or because none of them fired.
- **Never pool runs across deployments.** A table mixing an A10G row with an A100 row
  reports the machine, not the policy. `same-policies-across-gpus` exists to enforce this.
- **Repeats and spread, or no claim.** A single run has no error bar, and a difference
  inside run-to-run noise is not a difference.
- **Reject and defer are outcomes**, not losses. Goodput-under-admission counts them; raw
  throughput hides them.

## Do not

- Quote latency from a freshly started worker. The engine reports ready minutes before it
  serves, while it downloads weights and compiles graphs.
- Report a win without naming the regime it holds in. The question this project exists to
  answer is *which signal wins under which workload*, and an unqualified winner answers a
  question nobody asked.
