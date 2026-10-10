# 02-metrics

**What do these numbers mean, and what can they not tell you?**

Read before any experiment. Every wrong result in this project came from
reading a metric as the answer to a question it does not answer:

- a **lookup** counter taken for a **transfer** counter
- a **whole-card** KV figure quoted for a **slice** (15x out)
- decode's **own cache hit** read as a **KV hop**

Section 4 demonstrates each against the live cluster, so the two numbers that
look alike sit next to each other.

| section | |
|---|---|
| 1 | the catalogue — 23 metrics by the question they answer, 20 with a documented blind spot |
| 2 | the SLAs — each target, why that number, which metric proves it |
| 3 | percentiles — why histograms, and what p50/p90/p99 hide from each other |
| 4 | the traps — each one live |
| 5 | watch them move — own seeded load, with the real Grafana panels |

```bash
cd benchmarking/02-metrics
export GRAFANA_PASSWORD=...          # printed by make observability
../../.venv/bin/jupyter lab notebook.ipynb
```

Independent — drives its own load, needs nothing from 01. Just `make ssh`.

The catalogue lives in [`../prom.py`](../prom.py) rather than in cells, so the
queries are reviewable in one place and shared with every other notebook.
