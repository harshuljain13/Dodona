# notebook/

| | |
|---|---|
| [`01-sanity.ipynb`](01-sanity.ipynb) | **Run this before any load test.** Six checks, in order, each making the next meaningful. Writes `../reports/sanity-<date>.pdf` |
| [`utils.py`](utils.py) | shared by every notebook: `Cluster` reads the cluster, `Report` builds the PDF |

```bash
.venv/bin/pip install -r requirements-load.txt     # matplotlib, jupyterlab
make ssh                                            # leave the tunnel open
.venv/bin/jupyter lab benchmarking/notebook/
```

## Why PDFs come from matplotlib

`Report` renders pages as matplotlib figures via `PdfPages`. The obvious
alternative, `nbconvert --to pdf`, needs a LaTeX install — so it works on one
machine and fails on the next. This is uglier and always works.

## Why these are separate from `infra/`

Nothing here imports the control plane. A measurement tool that shares code
with the thing it measures can agree with it about something they are both
wrong about — `tests/test_load.py` enforces the boundary.

## Two values you have to fill in by hand

`01-sanity.ipynb` cannot read a pod's startup log through the ssh tunnel, so
two constants are typed in from `make logs`:

| notebook constant | log line |
|---|---|
| `KV_GIB_PER_POD` | `Available KV cache memory: X GiB` |
| `SEQ_PER_POD` | `Maximum concurrency for 16,384 tokens per request: Xx` |
| `MOONCAKE_TRANSPORT` | `Found N HCAs` and `Using … transport` |

They are the denominator of every concurrency claim in the submission. Taking
the whole-card figure instead of the per-slice one is how a pool of 4.9 gets
reported as 73.5.

## Latency measured here is indicative only

Through the tunnel your WAN round-trip is inside every number. For anything you
intend to quote, use `make load`, which runs on the box against
`127.0.0.1:8080`.
