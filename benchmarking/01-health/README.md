# 01-health

**Is the cluster there and wired?** Four checks, under a minute, no load and no
interpretation.

| | |
|---|---|
| topology | every component deployed at its expected replica count, read from kube-state-metrics rather than a manifest |
| health | each one ready, and whether it has been restarting |
| wiring | a request completes AND `orch_requests_total` moves — is the control plane in the path |
| observability | all five metric families actually scraped |

Ends with one line: safe to proceed, or not.

```bash
cd benchmarking/01-health
../../.venv/bin/jupyter lab notebook.ipynb
```

Needs `make ssh`. Independent of every other notebook.

## What it deliberately does not do

No percentiles, no SLOs, no load. A health check that also benchmarks is one
you stop running because it takes too long. [`02-metrics`](../02-metrics/) reads
what the numbers mean; `03-capacity` pushes them.
