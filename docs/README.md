# Deep dives

Two sets, and the split matters.

| | Covers | Index |
|---|---|---|
| [**`project/`**](project/) | **This project's decisions.** Capacity arithmetic, per-part design, the run protocol, the results report. Ours, and it carries the predictions | below |
| [**`cluster/`**](cluster/README.md) | **How the stack works.** Gateway, router, KV bus, observability, infrastructure — file by file. Documents code, so it stays accurate as the code does | [`cluster/README.md`](cluster/README.md) |

Start from [`../README.md`](../README.md), then
[`../SUBMISSION.md`](../SUBMISSION.md), then come here.

## Project-level — the parts

The brief asks for nine. One file each, so a part can be finished without
touching the others, and so the unfinished ones are visibly unfinished.

| Part | File | State | Cluster-side counterpart |
|---|---|---|---|
| — | [`project/architecture.md`](project/architecture.md) | the C4 levels: context, containers, inside the gateway | — |
| 0 — The application and the workload | [`project/00-use-case.md`](project/00-use-case.md) | ported, numbers pending | — |
| 1 — Capacity on paper | [`project/01-capacity.md`](project/01-capacity.md) | **rewritten for 1×A100 / 7B-AWQ** | — |
| 2 — Design the cluster | [`project/02-cluster.md`](project/02-cluster.md) | ported, numbers pending | [`infrastructure.md`](cluster/infrastructure.md), [`ARCHITECTURE.md`](cluster/ARCHITECTURE.md) |
| 3 — Guardrails, admit, stay vs leave | [`project/03-admission.md`](project/03-admission.md) | ported | [`gateway.md`](cluster/gateway.md) |
| 4 — Place | [`project/04-placement.md`](project/04-placement.md) | stub | [`router.md`](cluster/router.md) |
| 5 — Queue | [`project/05-queue.md`](project/05-queue.md) | stub — the notebook is the deliverable | [`gateway.md`](cluster/gateway.md) |
| 6 — Hop and warmth | [`project/06-hop.md`](project/06-hop.md) | ported, numbers pending | [`kvbus.md`](cluster/kvbus.md), [`kernels.md`](cluster/kernels.md) |
| 7 — Wire the app | [`project/07-wiring.md`](project/07-wiring.md) | ported | [`DATAFLOW.md`](cluster/DATAFLOW.md) |
| 8 — Proof | [`project/08-proof.md`](project/08-proof.md) | stub — fills in from the run | [`observability.md`](cluster/observability.md) |
| — | [`project/results-report.md`](project/results-report.md) | skeleton, every number `TBM` | — |

[`project/results-report.md`](project/results-report.md) is the document handed
in at the end. Parts 0–8 are the working notes behind it; `SUBMISSION.md` is the
part-by-part answer sheet that links to both.

## The convention that matters

Every number is marked **predicted** or **measured**.

Predictions are written down *before* the run and checked afterwards. They do not
get edited to match what happened — a prediction that was wrong is more
informative than one that was retrofitted, because being wrong by 2× tells you
which piece of arithmetic was misunderstood.

`TBM` means to-be-measured. A `TBM` left in the final report is a hole we are
disclosing, not a hole we missed.

The eight predictions live in
[`../SUBMISSION.md`](../SUBMISSION.md#predictions-written-before-the-run) and are
checked in [`project/08-proof.md`](project/08-proof.md).

## Honest state of these docs

`00`, `02`, `03`, `06`, `07` and `results-report` were written against an earlier
hardware target (4 × A100-40GB, `Qwen2.5-32B-AWQ`, 256 KiB/token) and **carry
stale numbers**. Their reasoning is intact; the arithmetic is not.
`SUBMISSION.md` and `project/01-capacity.md` are the authority for every figure
until they are reconciled.

Tracked rather than hidden, because a doc that silently disagrees with its own
project is worse than one that says so.
