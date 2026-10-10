# reports/

Generated run reports. Nothing here is source — it is all output, and it is all
reproducible from a command.

| | |
|---|---|
| `cluster-<name>.json` | `python3 infra/cluster/setup/report.py --json benchmarking/reports/cluster-<name>.json` |
| `cluster-<name>.txt` | the same run, as the text it printed |

`report.py` reads the live cluster and interprets it — every number comes with
what it tells you, and anything that looks wrong is called out with what to do.
The question it exists to answer, at the bottom of every run:

> was anything actually scarce, and could an admission policy have fired?

A latency number without that beside it cannot tell a policy that worked from
one that never had anything to decide.

Load-generator bundles live under [`infra/metrics/`](../../infra/metrics/), not here:
`infra/metrics/<run>/load.json` is the raw per-request record, this is the
interpretation.
