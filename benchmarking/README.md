# benchmarking/

Load generators. **Run them on the GPU box, not over the ssh tunnel.**

A tunnel puts your WAN round-trip inside every TTFT and serialises concurrency
through a single TCP stream, so a bundle taken from a laptop measures your
internet rather than the cluster. `make load` runs these on the box for exactly
that reason, then rsyncs the bundle back.

```bash
make load                              # 8 sessions, seed 1, run=baseline
make load ARGS="--sessions 32 --max-tokens 400 --run high"
```

## Layout

One folder per experiment, each self-contained — the notebook, its reports, its
screenshots. A shared reports/ at the top makes you cross-reference filenames to
work out which run produced what.

```
benchmarking/
  utils.py                 Cluster, Report — shared by every notebook
  prom.py                  Prometheus + Grafana, the metric catalogue
  slos.py                  what we promise
  load.py  locustfile.py   load generators, run ON THE BOX
  01-health/               is it there and wired?
  02-baseline/              what do the numbers mean?
```

**Every notebook is independent.** Each drives its own seeded load and needs
nothing from the one before it — so you can run 02 without 01, and a failure in
one does not invalidate another. Shared code lives in `utils.py` and `prom.py`,
never in a notebook another notebook has to have run.

| | |
|---|---|
| `load.py` | agent-shaped sessions: 4 sequential turns, a ~1k shared prefix, a growing context. Samples `vllm:kv_cache_usage_perc` throughout and ends with a verdict on whether anything was ever scarce |
| `locustfile.py` | Locust UI, for watching rather than measuring. Runs on the Mac against the tunnelled gateway, so `:8089` needs no forward |
| `crew_flood.py` | concurrent multi-role agents — the pattern that makes prefix caching matter |
| `agent_flood.py` | sustained RPS with abort behaviour |

## Why runs are comparable

`--seed` makes the workload deterministic, so two arms differ only by the thing
under test. A bundle lands in `../infra/metrics/<run>/load.json` with every
request's status, latency and token counts, plus the sampled KV signal.

**Nothing here imports the control plane.** A load generator that shares code
with the thing it measures can agree with it about something they are both
wrong about; `tests/test_load.py` enforces that boundary.

## The question every run has to answer

> was anything actually scarce, and could an admission policy have fired?

`load.py` prints it. A latency number without that beside it cannot tell a
policy that worked from one that never had anything to decide — which is the
difference between a result and a number.
