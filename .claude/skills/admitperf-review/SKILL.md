---
name: admitperf-review
description: >-
  Adversarial review for AdmitPerf changes, tuned to the defects this repository actually
  produces — results from runs where the signal never moved, and claims the bundles do not
  support. Use when reviewing a PR or a diff, or before marking anything done.
---

# Reviewing a change

## The defect this repo produces

Not broken code. **A number that looks like a result and is not one.**

The shape: a run completes, a table is produced, the table is cited — and the signal the
policy reads never left its floor, so the policy never acted and the table says nothing
about it. This has happened: `half-capacity-headroom`, `waiting_requests` identically zero
across three repeats, treated as banked evidence.

So on any change that produces or cites a number, ask first: **could this number have come
from a run where nothing was saturated?** If yes, that is the review finding.

## Checks, in order of what they catch

1. **Does a cited number name its bundle, and does that bundle name its commit?** A number
   traceable only to a version string that never changes is not traceable.
2. **Is the comparison within one deployment?** Pooling across hardware reports the
   machine, not the policy.
3. **Repeats and spread?** A single run has no error bar. A gap inside noise is not a gap.
4. **Does a new policy actually refuse anything** in its tests? One that admits everything
   passes most of them.
5. **Class A?** A policy that needs engine changes does not fit behind this API, and a
   review is the last cheap place to notice.
6. **Does `docs/status.md` still match what the code can demonstrate in one command?** The
   gap between claim and proof is the risk that matters for this project.

## On vendored code

`infra/` is the vendored serving cluster, excluded from ruff. Do not reformat it — that
makes every future diff against upstream unreadable. Changes there should be minimal and
should say why they could not be made upstream.

## What a good review comment looks like

Name the failing input, not the style. *"If `max_num_seqs` is below what KV allows, this
measures the scheduler rather than the cache"* is reviewable. *"Consider refactoring"* is
not.
