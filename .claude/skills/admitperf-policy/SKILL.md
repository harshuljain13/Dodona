---
name: admitperf-policy
description: >-
  Port a published admission policy into AdmitPerf — the Class A/B test that comes first,
  the frozen interface, declaring what signal it needs, and recording deviations from the
  paper. Use when adding a policy, porting one from a paper, or reviewing a port.
---

# Porting a policy

## Class A or Class B — decide before writing anything

**Class A** is a pure function of `(request, observable fleet state) → decision`, running in
front of an unmodified engine and reading its metrics. That is all this API accepts.

**Class B** changes how batches are *formed inside* the engine. Porting one means
maintaining a fork of thousands of lines of scheduler internals. FairBatching, FastServe
and CONCUR are Class B and do not fit here — saying precisely why is itself the
Portability axis the survey needs.

Getting this wrong costs the whole port, and it is answerable from the paper's abstract.

## The interface is frozen

```python
def decide(self, req: Request, state: SystemState) -> Decision
```

`ADMIT`, `DEFER` (with `retry_after_ms`), or `REJECT` (with a reason). Deterministic: the
same inputs and internal state must give the same decision, or experiments stop being
reproducible.

Anything the policy needs that `SystemState` lacks arrives as an **optional** field. Never
change `Decision` or `decide()`.

## Declare what it reads

A policy states its signal in the same vocabulary as the survey's axes — unit, setting,
objective, signal, portability. Two things fall out for free:

- The harness can **refuse to run** a policy against an engine that cannot supply its
  signal, before any provisioning cost.
- The report can show the **observed range** of that signal, which is what tells a reader
  the policy was live.

## Write the deviation list while porting, not after

Anything that cannot be reproduced faithfully: a simulator-only assumption, a metric the
engine does not expose, a constant the paper never states. Written during the port it is a
record; written afterwards it is a reconstruction, and the details that mattered are gone.

## Before calling it done

- A test that the policy refuses *something* under pressure. A policy that admits
  everything passes most tests.
- A run where its signal actually crossed its threshold. Otherwise the port is unexercised
  and you have no evidence it works.
- Its entry in `docs/policies.md`, with the deviation list.
