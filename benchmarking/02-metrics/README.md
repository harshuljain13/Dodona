# 02-metrics

**What do these numbers mean, and what can they not tell you?**

Read before any experiment. Several metrics answer a narrower question than
their name suggests, and the difference decides whether a result is real:

- `external_prefix_cache_hits_total` records that the **lookup** matched. The
  pull happens after and can fail.
- `prefix_cache_hits_total` is decode's **own** cache — one word away from the
  connector metric, and a high rate there looks like a working hop.
- `prompt_tokens_total` counts tokens **in the request**, not work done. It does
  not move when the KV hop starts working.
- `kv_cache_usage_perc` is a ratio of **one slice**, not the card — 15x apart.

| section | |
|---|---|
| 1 | the catalogue — 23 metrics by the question they answer, 20 with a documented blind spot |
| 2 | the SLAs — each target, why that number, which metric proves it |
| 3 | percentiles — why histograms, and when a percentile is not trustworthy |
| 4 | component checks — KV connector, prefill/decode split, slice capacity |
| 5 | watch them move — own seeded load, with the real Grafana panels |

```bash
cd benchmarking/02-metrics
export GRAFANA_PASSWORD=...          # printed by make observability
../../.venv/bin/jupyter lab notebook.ipynb
```

Independent — drives its own load, needs nothing from 01. Just `make ssh`.

The catalogue lives in [`../prom.py`](../prom.py) rather than in cells, so the
queries are reviewable in one place and shared with every notebook.

## On percentiles

`histogram_quantile` interpolates **inside a bucket**. When the data is
concentrated it returns the bucket edge, and two different metrics sharing
bucket boundaries then report identical percentiles while their means differ by
4x. Section 3 shows the exact mean from `_sum/_count` beside every percentile
and fails if two metrics collide, because that is the signal you need more load
before quoting a p99.
