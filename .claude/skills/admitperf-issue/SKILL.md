---
name: admitperf-issue
description: >-
  File an AdmitPerf issue that is actionable on arrival — which template, the evidence to
  gather before writing it, and the labels. Use when asked to file an issue, open a bug,
  track something, or when a problem is found that will not be fixed in the current change.
---

# Filing an issue

## Gather the evidence first

For anything about a run, three things answer most of what would otherwise be asked, and
each round trip of asking costs a day:

1. **`manifest.json`** — what ran, against what, at which commit.
2. **The signal range from `summary.json`.** Usually *the* answer. A policy whose signal
   never approached its threshold did not misbehave; it never fired, and it reports the same
   numbers as no policy at all.
3. **The command**, and the config it ran against.

If the signal range explains the behaviour, say so in the issue rather than filing it as a
bug. "The policy admitted everything" and "the policy never saw pressure" look identical
from outside and are completely different problems.

## Which template

| Template | For |
|---|---|
| **Bug report** | Something behaves differently from what it says it does |
| **Policy port** | A published policy that should be available here |
| **Documentation** | Wrong, missing, or misleading — misleading is the worst, the reader leaves confident |

Blank issues are off on purpose. The templates ask for what we would otherwise have to
chase.

## Labels

`policy` · `infra` · `measurement` · `blocked-on-gpu` · `bug` · `documentation` ·
`enhancement`

**`blocked-on-gpu` matters most.** It separates what can be done on a laptop today from what
waits on a rented cluster. Without it the board looks full while everything on it is
unstartable.

## Writing it

**One issue, one thing.** An issue that contains three problems gets closed when one is
fixed.

**Say what you expected, not just what happened.** *"It admitted everything"* is a
description. *"It admitted everything, and I expected refusals once KV passed 0.9"* is a
specification, and someone can tell whether it is fixed.

**Attach it to the milestone** if it belongs in the current one. An issue with no milestone
is invisible to anyone planning.

## Before filing

A problem found mid-change that you are about to fix does not need an issue. A problem found
mid-change that you are **not** going to fix does — otherwise it survives as a comment in a
diff nobody reads again.
