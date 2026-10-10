# Part 5 — Queue

**Status: stub.** Fills in from the run, with a notebook.

## The thing worth being clear about

There are two queues and they are not the same queue.

| Queue | Where | Who waits | How it is observed |
|---|---|---|---|
| vLLM's waiting queue | inside the engine | admitted requests, waiting for a block allocation | `vllm:num_requests_waiting` |
| the gateway's | **does not exist** | requests block on a thread | nothing to observe |

The gateway holds no queue of its own. That is why `queue_depth` is scraped from
`/metrics` rather than read out of a local data structure, and why a `defer`
verdict means "come back" rather than "you are now 12th in line".

So a request that the policy **deferred** and a request that vLLM **queued** are
in different places, for different reasons, and a report that adds them together
is reporting a number that does not exist.

## The question for the run

Who is actually sitting where when the fleet saturates? Three possibilities, and
which one happens tells you whether admission is doing anything:

1. **Admission defers, vLLM's queue stays near zero** — the policy is absorbing
   the burst. Admission is load-bearing.
2. **Admission admits everything, vLLM's queue grows** — the signal never
   reached the threshold. The policy was inert and the engine took the strain.
3. **Both** — the threshold is set below where the engine starts queueing, so
   the policy and the engine are both shedding, and the policy's refusals are
   premature.

Prediction [P1](01-capacity.md#predictions-written-before-the-run) says
`kv_cache_usage_perc` saturates before `num_requests_waiting` grows, which means
(1) is expected.

## To fill in from the run

| | Low load | High load |
|---|---|---|
| `num_requests_waiting` p50 / max | *TBM* | *TBM* |
| Deferred by policy | *TBM* | *TBM* |
| Rejected by policy | *TBM* | *TBM* |
| Preemptions | *TBM* | *TBM* |

A preemption count above zero means the engine ran out of blocks for a sequence
it had already started — which is the engine doing admission control after the
fact, and the clearest possible evidence that the gate let through more than the
fleet could hold.
