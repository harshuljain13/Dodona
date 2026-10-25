# Project skills

Five skills that encode how Dodona is worked on. They load automatically for anyone using
Claude Code in this repository — no setup, because they are versioned alongside the code
they describe.

| Skill | Use it when |
|---|---|
| `dodona-stage` | Bringing up or debugging a stage from `infra/lambda-cluster/RUNBOOK.md` |
| `dodona-capacity` | A knob moved — card, model, slice size, context — and the arithmetic has to be redone |
| `dodona-run` | Running an experiment, or reading what one produced |
| `dodona-docs` | A number changed and the docs have to stop disagreeing with each other |
| `dodona-review` | Reviewing a diff, or before calling anything done |

They exist because this project has a specific way of being wrong: **numbers that no longer
match the hardware, and claims no scrape supports.** Each skill is aimed at one of those.
