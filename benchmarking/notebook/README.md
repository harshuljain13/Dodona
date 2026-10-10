# benchmarking/notebook/ — Part 5

The brief submits this separately: *"Github Link along with the notebook from
part 5."* So it is a graded artifact in its own right, not an appendix.

| | |
|---|---|
| `part5-queue.ipynb` | **Not written.** Answers [Part 5](../../docs/SUBMISSION.md#part-5--queue) from live scrapes |

## What it has to show

Every answer in Part 5 needs a scrape, not an assertion.

1. **Our queue vs vLLM's waiting queue** — side by side, same timestamp. The
   point is that ours is empty because it does not exist: requests block on a
   thread and the engine does the queueing.
2. **waiting / running / swapped** across all three pods, sampled during a burst.
3. **`orch_replica_queue_depth` per pod**, under two mixes — shared-prefix and
   unique-prefix — to show which pod absorbs which.
4. **Ordering** — a 16k retrieve and a short agent step arriving together. Show
   our admit/place decision and then the engine's batch composition, so it is
   visible which layer ordered what.
5. **Paging vs prefix reuse** — `prefix_cache_hit_rate` against block-table
   occupancy on the shared-prefix mix. Paging prevents fragmentation; the prefix
   cache is what dedupes the ~1k shared prompt.
6. **Engine flags** — the values used, and the KV-vs-scheduler arithmetic showing
   why `max_num_seqs 64` sits above the KV limit of 5.5 on purpose.
7. **KV full after admit** — `vllm:num_preemptions` over a run. Non-zero means
   the gate admitted more than the fleet could hold.
8. **Aborted client** — kill a request mid-generation, watch
   `kv_cache_usage_perc` return. **Common in a voice app**: a listener who talks
   over the answer aborts mid-generation.
9. **Ramp vs slam** — TTFT on a just-returned worker, cold then warm, showing why
   only the warm number is quotable.

## Conventions

Scrapes are saved under [`infra/metrics/`](../../infra/metrics/) as raw text, so a number in
the notebook can be traced to the scrape it came from. Plots go to
[`../assets/screenshots/`](../../assets/screenshots/).

A cell that asserts without a scrape behind it is the thing this notebook exists
to prevent.
