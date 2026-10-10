# Part 4 — Placement

**Status: stub.** Placement is deliberately simple — it is not what we are
measuring — but the brief asks for it and one limitation needs stating rather
than implying.

## What it does

`cluster/router/router.py`. Two replicas per pool, so there is a real choice.

| Pool | Scored on | Reason |
|---|---|---|
| prefill | prefix-cache locality, token load, KV headroom | A worker holding the 1k shared system prompt saves re-prefilling it |
| decode | active requests, KV headroom | Prefix is irrelevant; the KV arrives over the hop regardless |

`least_loaded` within those signals. Not tuned, not compared against
alternatives, and the report will not claim it is good.

## The limitation

Both workers are chosen **before** prefill runs. A 12k prefill takes on the order
of a second, and by the time it finishes the decode worker we picked may no
longer be the least loaded one. Deciding the decode destination at transfer time
would place better.

Not fixing it in this project. It is named here so that any decode-side
imbalance in the run is attributed to this rather than to the admission policy.

## To fill in from the run

| | |
|---|---|
| Requests per prefill replica | *TBM* |
| Requests per decode replica | *TBM* |
| Prefix-match rate at the router | *TBM* |
| Worst decode imbalance observed | *TBM* |

If the decode split is worse than roughly 60/40 under load, the
choose-before-prefill limitation is the first suspect, and the way to tell is
whether the imbalance grows with prefill size.
