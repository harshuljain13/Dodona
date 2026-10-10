# reports/

Generated run reports. Nothing here is source — it is all output, and it is all
reproducible from a command.

**One file per notebook, overwritten on each run.** A timestamped file per run
leaves a directory of near-identical PDFs and no answer to "which one is
current". The run time is on the first page of each.

| | produced by |
|---|---|
| `01-sanity.pdf` | `notebook/01-sanity.ipynb` |
| `02-capacity.pdf` | `notebook/02-capacity.ipynb` *(not written yet)* |
| `99-report.pdf` | the synthesis — every proof, in order *(not written yet)* |
| `cluster-<stamp>.json` | `python3 infra/cluster/setup/report.py --json` |

`report.py` reads the live cluster and interprets it — every number comes with
what it tells you, and anything that looks wrong is called out with what to do.
The question it exists to answer, at the bottom of every run:

> was anything actually scarce, and could an admission policy have fired?

A latency number without that beside it cannot tell a policy that worked from
one that never had anything to decide.

Load-generator bundles live under [`infra/metrics/`](../../infra/metrics/), not here:
`infra/metrics/<run>/load.json` is the raw per-request record, this is the
interpretation.
