# 01-sanity

**Run this before anything else.** A load test against a half-up fleet measures
the half that is up.

| | |
|---|---|
| `notebook.ipynb` | nine sections: question, chart, conclusion |
| `reports/` | `01-sanity.pdf` and `01-sanity.html`, overwritten each run |
| `screenshots/` | Grafana captures worth keeping with this run |

```bash
cd benchmarking/01-sanity
../../.venv/bin/jupyter lab notebook.ipynb          # or Run All
```

Headless, which is what produces the committed artefacts:

```bash
../../.venv/bin/jupyter nbconvert --to notebook --execute --inplace notebook.ipynb
../../.venv/bin/jupyter nbconvert --to html --output-dir=reports --output 01-sanity notebook.ipynb
```

Needs the tunnel (`make ssh`). Latency measured here includes your round-trip
to the box — read the shape, not the value. `make load` runs on the box.

## Two constants you type in by hand

A notebook cannot read a pod's startup log through the tunnel:

| constant | from |
|---|---|
| `KV_GIB_PER_POD` | `make logs \| grep 'Available KV cache memory'` |
| `SEQ_PER_POD` | `make logs \| grep 'Maximum concurrency'` |

They are the denominator of every concurrency claim. Taking the whole-card
figure instead of the per-slice one is how a pool of 4.7 gets reported as 73.5.

## What it is actually for

Six verdicts, but one question: **is the control plane in the request path, and
does the KV hop move bytes?** `make kv` answers the second faster; this puts it
beside the capacity arithmetic and the latency curve that should agree with it.
