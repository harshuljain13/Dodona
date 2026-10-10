---
name: admitperf-debt
description: >-
  Audit what AdmitPerf claims against what its bundles prove — numbers without a bundle,
  policies never exercised, docs describing code that moved. Use when asked about technical
  debt, what is unproven or over-claimed, or before a release or a paper submission.
---

# Auditing claims against proof

## What this looks for

**A claim the software cannot demonstrate in one command.** Reviewers read the repo; a gap
between `docs/status.md` and what runs is the risk that matters here.

## The audit

1. **Every number in a doc names a bundle.** Walk the docs, list the figures, and for each
   one find the run it came from. A number with no bundle is either unverified or lost, and
   both are reported the same way: as unproven.
2. **Every cited bundle names its commit.** Traceable only to a version string that never
   changes is not traceable.
3. **Every registered policy has a run where its signal crossed its threshold.** A policy
   never exercised under pressure is a policy with no evidence behind it, however many
   tests it passes.
4. **Every claimed capability has a command that shows it.** If showing it needs three
   steps and local knowledge, it is not demonstrable.
5. **Docs against the tree.** This repo has moved its package layout more than once;
   anything citing a path is suspect until checked.

## Known debts, so they are not rediscovered

- **A published figure its bundles did not support.** The reason this audit exists.
- **`half-capacity-headroom` isolates nothing.** Engine never saturated, every signal flat.
  Any claim resting on it is unsupported.
- **KV pressure and queue depth have never been compared in the same deployment.**
  `which-policy-when` is specified for it and has not run.
- **`infra/` is vendored and unlinted.** Its lint debt is upstream's; ours is that we have
  not decided what happens to the gateway inside it.

## How to report

Per item: the claim, where it is made, what would prove it, and whether that evidence
exists. **An item that cannot be proven is reported as not-shown, never quietly dropped** —
dropping it is how an over-claim survives an audit that was meant to catch it.
