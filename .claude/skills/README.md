# Project skills

Eight skills that encode how this project is worked on. They load automatically for anyone
using Claude Code in this repository — no setup, because they are versioned alongside the
code they describe.

| Skill | Use it when |
|---|---|
| [`admitperf-run`](admitperf-run/SKILL.md) | Running an experiment, or reading what one produced |
| [`admitperf-policy`](admitperf-policy/SKILL.md) | Porting a published admission policy |
| [`admitperf-review`](admitperf-review/SKILL.md) | Reviewing a change, adversarially |
| [`admitperf-pr`](admitperf-pr/SKILL.md) | Opening a pull request |
| [`admitperf-issue`](admitperf-issue/SKILL.md) | Filing a bug, a policy port, or a docs problem |
| [`admitperf-debt`](admitperf-debt/SKILL.md) | Auditing claims against the bundles behind them |
| [`admitperf-spec`](admitperf-spec/SKILL.md) | Planning docs, the board, and what is actually done |
| [`admitperf-release`](admitperf-release/SKILL.md) | Cutting a release |

## Why these exist

They are not style guides. Each encodes a mistake this project actually made.

- **`admitperf-run`** exists because of `experiments/half-capacity-headroom`:
  `waiting_requests` identically zero across all three repeats, `running_requests` peaking
  at 6-8 against a cap of 10. The engine was never saturated, so every signal was flat —
  not just the one under test. That run was treated as banked evidence for weeks. It
  isolates nothing.
- **`admitperf-policy`** front-loads the Class A / Class B question, because a Class B
  policy cannot sit behind this API at all and discovering that mid-port wastes the port.
- **`admitperf-debt`** exists because a figure was published that its bundles did not
  support, and because `docs/status.md` has disagreed with what the software can actually
  demonstrate.
- **`admitperf-pr`** says to check the gate's *exit codes*: piping `ruff` into `tail`
  returns tail's status, and a formatting failure can be committed while the gate reports
  clean. It also says to run the gate in a clean venv, because CI has already failed on an
  extra the working venv had accumulated by hand.
- **`admitperf-issue`** asks for the signal range before anything else, because "the policy
  admitted everything" and "the policy never saw pressure" look identical from outside.
- **`admitperf-spec`** exists because the plan and the board have disagreed: a direction was
  recorded as banked while the run behind it isolated nothing.

The thread running through all eight: **a number is not a result until you know the signal
moved.** A policy that never saw its threshold did not perform badly — it never ran, and it
reports the same numbers as no policy at all. Telling those apart is the entire point of
this repository, so it is the first thing every skill checks.

## Adding one

A skill earns its place by encoding something that went wrong, or a rule a newcomer would
otherwise learn by breaking. Keep it short, make it concrete, and cite the real incident —
the specifics are what make a rule memorable and what let a reader judge whether it still
applies.
