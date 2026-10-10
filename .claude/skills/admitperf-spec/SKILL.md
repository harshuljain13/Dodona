---
name: admitperf-spec
description: >-
  Work with AdmitPerf's planning docs and the issue board — which one is authoritative,
  reconciling a claim against the tree before calling anything done, and recording
  deviations. Use when asked what is done, about phases or tasks, or when closing work.
---

# Planning, and what is actually done

## Two places track work. Only one is authoritative.

- **GitHub issues and the milestone** — the source of truth for status. What is open, what
  is blocked, what shipped.
- **`.spec-dev/`** — local planning, deliberately not in git. Requirements, design
  decisions, task breakdowns. It explains *why*; it does not record *whether*.

When they disagree, the board wins and the plan gets updated. A plan that claims work is
done is how this project once recorded a direction as "banked" while the run behind it
isolated nothing.

## `.spec-dev/` contents

```
requirements.md         the thesis, the goals, what would make it wrong
spec.md                 the active design decisions
spec-paperB-stale.md    superseded, kept because its findings are still true
tasks.md                phases, each one sitting
```

**A stale plan is marked stale, not deleted.** `spec-paperB-stale.md` carries the
half-capacity finding, which is still the most important thing in that file.

## Before calling anything done

**Reconcile against the tree, not against the plan.** The question is never "did I write
this down as finished" but "can I show it in one command".

- For a policy: a run where its signal crossed its threshold. Tests passing is not that.
- For a measurement: the bundle, and the commit that produced it.
- For a claim in a doc: the command that demonstrates it.

Anything that cannot be shown is reported as **not-shown**, never quietly dropped. Dropping
it is how an over-claim survives.

## Recording a deviation

When the work diverges from the plan — and it will — write the deviation where the plan is,
with the reason, at the time. Afterwards it is a reconstruction and the detail that mattered
is already gone.

The deviations worth recording are the ones where something turned out to be impossible or
unnecessary, not the ones where an estimate was wrong.

## Closing

An issue closes when its stated "done when" is true, not when the code was written. Several
issues on this board have a "done when" that requires a live cluster; those cannot close
from a laptop, and marking them done because the code exists is the failure mode the
`blocked-on-gpu` label was created to prevent.
