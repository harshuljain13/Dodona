# Part 6 — Hop and warmth

**Status: stub.** The arithmetic below is **predicted** and written before the
run; the measurements fill in after.

## Why all four workers sit on one node

A 12k-token prompt produces 3.0 GiB of KV (256 KiB/token × 12,288). The question
is whether moving those bytes beats rebuilding them.

```
recompute        12k tokens of prefill on an A100-40 at int4   ~1.0-1.5 s   (predicted)
move, NVLink     3.0 GiB at ~250 GB/s effective               ~12 ms
move, 25 GbE     3.0 GiB at ~3 GB/s effective                 ~1.0 s
```

On NVLink the hop is ~100× cheaper than recomputing. Over commodity Ethernet the
two are within noise of each other, and once you add the store round trip
**re-prefilling is faster than hopping**. A multi-node fleet would make the hop
theatre: it would appear in the dashboards, move real bytes, and cost more than
the work it saved.

That is the whole reason for the one-node constraint in
[Part 1 step 1](01-capacity.md#1--gpu-1--a100-40gb-one-node). It is not a
convenience.

## What disaggregation buys, and what it costs

Stated plainly because the cost is large and
[Part 1 step 6](01-capacity.md#6--replicas-and-topology-1-prefill--2-decode)
accepted it.

| | Cost | Benefit |
|---|---|---|
| Concurrency | 7.8 instead of 15.6 — a prefill worker's 15.6 GiB churns but never holds a live sequence | — |
| Interference | — | A 12k prefill does not stall the decode loop of every sequence sharing that GPU |
| Scaling | — | The two pools scale independently, and the dashboards point at a pool rather than at "the cluster" |

The benefit to look for in the run is **inter-token latency variance**, not
throughput. Disaggregation's case is that decode stays smooth while large
prefills arrive; if inter-token p95 is as spiky as an aggregated fleet's would
be, the hop is not earning its concurrency cost.

## Warmth

Two different things get called warmth and only one of them is about the hop.

| | What it is | When it matters |
|---|---|---|
| **Worker warmth** | `torch.compile` has captured its CUDA-graph buckets | At startup. `READY` appears minutes before the engine can actually serve |
| **Cache warmth** | the shared prefix is resident in a worker's prefix cache or in Mooncake | After any hop, and after any eviction |

Worker warmth is a measurement hazard: any TTFT taken off a cold worker is
meaningless. The check is a fixed warmup request per worker, then re-quote.
`cluster/router/warmup.py` quantifies the trade — capturing every bucket costs
~18 s of startup for a 40 ms first TTFT; one bucket is ready far sooner and costs
the first user ~1.8 s.

Cache warmth is checked after any hop: did the decode worker actually receive the
blocks, or did it re-prefill silently? The only honest way to know is Mooncake's
`/hops` endpoint, and **only if `KV_BACKEND=mooncake`** — `nccl.py` and `nixl.py`
return immediately without moving anything, so an empty hop panel under those
backends means nothing at all.

## To fill in from the run

| | |
|---|---|
| Hops recorded at `/hops` | *TBM* |
| Bytes moved | *TBM* |
| Measured recompute cost for a 12k prefill | *TBM* |
| Measured transfer time, same payload | *TBM* |
| Inter-token p95, disaggregated | *TBM* |
| Prefix-cache hit rate, prefill pool | *TBM* |
| Failed writes / evictions in the store | *TBM* |

If hops recorded is zero while requests succeed, check `KV_BACKEND` before
concluding anything.
