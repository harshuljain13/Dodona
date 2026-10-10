---
name: admitperf-pr
description: >-
  Open a pull request against AdmitPerf — the gate to run, what the body must explain, and
  the branch rules. Use when asked to open or raise a PR, or when finishing a change that
  is ready to land.
---

# Opening a PR

## Run the gate, and check exit codes

```bash
ruff check . && ruff format --check . && pytest -q
```

**Check the exit code, not the output.** Piping either into `tail` returns tail's status,
and a failure then looks clean. This is how a formatting failure gets committed.

Run it in a **clean environment**, not your working venv. A narrower install than CI uses
collects tests and then fails to import them — which has already shipped here once, when
`.[dev]` passed locally only because the venv had accumulated `pandas` and `streamlit` by
hand. Use `pip install -e '.[all]'` in a fresh venv, which is what CI does.

## Branch and title

Branch from `main`. Never commit to it directly.

Conventional-commit title, because release notes are assembled from these:
`feat(policies):` · `fix(bench):` · `refactor:` · `docs:` · `ci:`

## The body

**What changed, and why.** The *why* is the part worth writing — a change that explains its
reasoning is understandable in six months. If you fixed something, describe the broken
behaviour from outside: the symptom someone would have reported.

**How it was verified.** Not "tests pass". What you actually drove — a run you watched, a
bundle you read, the decision log you checked. This project has shipped a figure its
bundles did not support and a run where every signal was flat. Both passed their tests.

**Anything deliberately left undone.** Known gaps and follow-ups. A limitation stated here
is a known limitation; the same limitation unstated is a bug waiting to be rediscovered.

## Before marking ready

- Any number in the diff names the bundle it came from.
- A new policy is Class A and declares its signal.
- Version untouched — releases are cut by tag, never by editing a version by hand.
- Linked to its issue and milestone, so the board reflects reality.
