# What is real, what is simulated, and why

Every control-plane component, checked against the code rather than the docs.
Written because "I don't trust any of your code" is a fair position when nothing
has demonstrated a benefit.

**How to read the verdicts.**

| | |
|---|---|
| **REAL** | works against a live vLLM engine. Can be proved with a measurement |
| **DEGRADED** | runs, but a signal it depends on is `None` against real vLLM, so it silently contributes nothing |
| **SIMULATED** | models the behaviour. Faithful against `FakeWorker`, inert against a real engine |
| **STUB** | returns immediately, does nothing |

---

## The inventory

| Component | Against real vLLM | Why |
|---|---|---|
| `inspect()` guardrails | **REAL** | A pure function on the payload. Backend-independent |
| tenant window | **REAL** | Token accounting in the gateway |
| `kv_threshold` admission | **REAL** | Reads `vllm:kv_cache_usage_perc`, which is live and per-pod |
| `queue_depth` admission | **REAL** | Reads `vllm:num_requests_waiting` |
| decode placement (`least_loaded`) | **REAL** | Scores on `active_requests` and `kv_free_ratio`, both populated from real metrics |
| **prefill placement (`prefix_then_load`)** | **DEGRADED** | See below — the prefix scorer returns `None` |
| `score_token_load` | **DEGRADED** | `tokens_in_flight` is set to the *request count*, not a token count |
| staleness filter (`age > 5s`) | **DEGRADED** | `VLLMWorker.snapshot()` hardcodes `age_s=0.0`, so a stale snapshot is never dropped |
| `kvbus.transfer()` — the hop | **SIMULATED** | Records the hop; does not move blocks |
| KV eviction | **SIMULATED** | Operates on `KVBus`, which holds nothing real for a `VLLMWorker` |
| `nccl.py`, `nixl.py` | **STUB** | Four lines, return immediately |
| overflow gate | **REAL** | An HTTP call to a provider |
| `FakeWorker` | **SIMULATED by design** | Exists so the control plane runs on a laptop with no GPU |

---

## The one that matters: prefix-aware placement does nothing

`control/router/router.py`:

```python
def score_prefix_cache(snap, req):
    if snap.prefix_tokens is None:
        return None                     # ← always, against real vLLM
    hit = snap.prefix_tokens.get(req.prefix_hash, 0)
    return hit / req.prompt_tokens

PREFILL_SCORERS = [score_prefix_cache, score_token_load, score_kv_utilisation]
```

`control/router/pools.py`, in `VLLMWorker.snapshot()`:

```python
return Snapshot(
    kv_free_ratio=kv_free,          # real
    active_requests=active,         # real
    queue_depth=waiting_i,          # real
    uncached_prefill_tokens=None,   # ← never populated
    prefix_tokens=None,             # ← never populated
    age_s=0.0,                      # ← hardcoded
)
```

So against a real engine **the highest-weighted prefill scorer always returns
`None`** and contributes nothing. Prefill placement falls back to token load
(itself degraded) and KV utilisation. The prefix-aware routing that
`SUBMISSION.md` Part 4 describes, and that the ~1k shared prefix is supposed to
exploit, is **inert**.

`FakeWorker` populates both fields — `prefix_tokens=self.bus.prefixes_for(self.id)`
— so every test of prefix stickiness passes, and the behaviour is real in
simulation and absent in production. **That is the worst shape a gap can have:
green tests over a dead code path.**

### Why it is like this

Not an oversight in the lab. `KVBus` tracks which worker holds which prefix
because in simulation it *is* the source of truth. Against real vLLM the engine
owns its own prefix cache, and the router has no way to read it — vLLM exposes
`prefix_cache_hits_total` and `queries_total` as **fleet counters**, not a
per-prefix map. There is no API that answers "do you hold prefix `abc123`".

So the router would have to infer it from its own history of what it sent where.
`KVBus.record()` already maintains exactly that. It is simply never surfaced to
`VLLMWorker.snapshot()`.

### What would make it real

One line, plus honesty about what it means:

```python
prefix_tokens=self.bus.prefixes_for(self.id),   # what WE sent here, not what vLLM holds
```

That turns the scorer from inert into a *heuristic*: "I sent this prefix here
recently, so it is probably still cached." Which is what a router can know. It
is not ground truth, and the docs must say so — vLLM may have evicted it.

**And it becomes provable:** route with the scorer on and off, measure
`vllm:prefix_cache_hits_total / queries_total` on the prefill pool. If the
heuristic works, the hit rate rises. If it does not, we have measured that and
can say so.

---

## How to prove each component, rather than assert it

Every row needs an A/B where the component is the only variable.

| Component | Experiment | Metric that moves if it works |
|---|---|---|
| guardrails | send 20% malformed | those requests never reach a pod: `vllm:prompt_tokens_total` flat for them, 400s at the edge |
| tenant window | one tenant at 3× its share | 429s for that tenant only, others unaffected |
| admission (`kv_threshold`) | same load, policy on vs `no_admission` | `vllm:num_preemptions_total` → 0 with it on; shed count > 0; p95 latency lower |
| decode placement | `least_loaded` vs random | load spread across the decode pool narrows |
| prefix placement | scorer on vs off | `prefix_cache_hits/queries` on prefill rises |
| the hop | colocated vs split | **currently unprovable** — see [`topology.md`](topology.md) |
| warm gate | TTFT on a just-restarted pod, ramped vs slammed | first-token latency |
| overflow | drive past capacity | 503s leave, 429s do not |

**Not one of these has been run.** That is the honest state: the capacity
arithmetic is measured and confirmed, the cluster serves, and **zero components
have had their benefit demonstrated.**

---

## What this changes

1. **`SUBMISSION.md` Part 4 overstates.** It describes prefix-aware prefill
   placement as if it works. Against real vLLM it does not.
2. **The green test suite is not evidence.** 113 tests pass, and the prefix
   tests pass *because* they run against `FakeWorker`. A test that only
   exercises the simulator cannot catch a signal that is `None` in production.
3. **Part 8 is the whole submission.** Everything above is plumbing until an A/B
   shows a component changing an outcome.
