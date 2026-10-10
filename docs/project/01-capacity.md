# Part 1 — Capacity: how we got to the configuration

The deep version of
[`SUBMISSION.md` Part 1](../../SUBMISSION.md#part-1--capacity-on-paper). **This
file is the authority for every number in the project.**

Six decisions, in order. Each narrows the next.

The hardware is **1 × A100-40GB on one Lambda node**, and the binding fact about
it is architectural rather than numerical: **A100 is sm80 and has no fp8 tensor
cores.** The usual move — quantize the weights to fp8, keep the KV cache in fp16
— does not exist here. That single fact forces step 4.

---

## 1 · GPU: 1 × A100-40GB, one node

What is obtainable. Two rejections worth recording:

- **H100-80GB** — not available. It would have changed step 4 completely, since
  sm90 has fp8 and a 7B at fp8 is 7.1 GiB rather than 5.2 at int4 — close enough
  that the *forced* quantization becomes a *chosen* one.
- **Four cards** — not available, and a module built on four cards is a result no
  cohort mate or workshop attendee could reproduce. Reproducibility on one card
  is a design requirement, not a consolation.

**One node matters** because the hop depends on the interconnect, and here the
hop happens *inside* one card between two slices. See [Part 6](06-hop.md).

## 2 · Model: `Qwen2.5-7B-Instruct`

28 layers, 28 Q heads, **4 KV heads**, `head_dim` 128.

```
kv_bytes_per_token = 2 (K,V) x 28 layers x 4 kv_heads x 128 head_dim x 2 B
                   = 57,344 B  =  56 KiB per token
```

### The brief asks what switching models does to bytes/token

| Considered | layers × kv_heads | KV/token | vs chosen | Verdict |
|---|---|---|---|---|
| **`Qwen2.5-7B`** | 28 × 4 | **56 KiB** | — | **chosen** |
| `Qwen2.5-3B` | 36 × 2 | 36 KiB | 0.64× | works (pool ~15) but a weaker researcher |
| `Qwen3-4B` | 36 × 8 | 144 KiB | **2.6×** | 1.1 seq/replica — barely serves one |
| `Qwen2.5-14B` | 48 × 8 | 192 KiB | 3.4× | 0.3 seq/replica — refuses |
| `Qwen2.5-32B` | 64 × 8 | 256 KiB | 4.6× | 18.1 GiB int4 will not fit a 12 GiB slice |

**Parameter count does not predict cache cost.** `Qwen3-4B` is 47% smaller than
our 7B and costs 2.6× more per token, because it has twice the KV heads and eight
more layers. This is the single most useful thing on this page.

Two corollaries that are easy to get backwards:

- **Quantization changes weights, not bytes/token.** int4 shrinks the 7B's
  weights from 14.19 GiB to 5.2; the KV cache is still 56 KiB/token, because
  `kv_cache_dtype` is a separate setting and we leave it at fp16.
- **Architecture is the only lever on bytes/token.** A model with interleaved
  local/global attention — Gemma 3's 5:1 sliding-window pattern, say — bounds
  most of its layers to a fixed window, so its *growing* cost per token can be
  lower than a dense 7B's despite being far larger. Worth knowing; not available
  to us within the Qwen family.

### Why 7B and not 3B

3B fits comfortably and gives a decode pool of ~15. We took the 7B anyway,
because the research task — read a 12k-token source, synthesise, cite accurately
— is the kind a 3B does visibly badly, and an app nobody believes makes the
serving numbers uninteresting.

The lab's 3B-at-2,048 run is the cautionary case in the other direction: a
sequence there is ~16× lighter than ours, the cache never fills, and no admission
policy could have fired regardless of its threshold.

## 3 · Does it fit at bf16? No

Working in GiB throughout. A100-40GB reports 40,960 MiB = **40.0 GiB**; with
three pods, CUDA contexts and HAMi overhead leave **~37 GiB allocatable**.

| Slicing | `gpumem` | budget (×0.90) | 7B bf16 weights | KV left | seq @16k |
|---|---|---|---|---|---|
| 3 slices | 12 GiB | 10.80 | 14.19 | — | **will not load** |
| 2 slices | 18 GiB | 16.20 | 14.19 | 0.81 | **0.93 — refuses** |
| 1 whole card | 37 GiB | 33.30 | 14.19 | 17.6 | 20.1, but one worker |

**The middle row is the trap and it deserves the detail.** 7B bf16 in an 18 GiB
slice *does* load: 14.19 GiB of weights against a 16.20 GiB budget, with 0.81 GiB
left over. The pod reports `READY 1/1`. Then one 16k sequence needs 0.875 GiB,
and every request that arrives is refused.

A config check that only compares weights against available memory lets this
straight through. **The check has to require room for at least one sequence at
`max_model_len`:**

```
weights + activations + (kv_bytes_per_token x max_model_len)  <=  gpumem x 0.90
```

The third row is the other thing worth seeing: one whole-card worker would serve
20 concurrent sequences — nearly double the fleet we actually build. We give that
up deliberately, and step 6 is where that trade is paid.

## 4 · Quantize to AWQ int4 — forced, not chosen

**fp8 is not an option.** A100 is sm80; fp8 tensor cores arrive with sm89/sm90.
On this card fp8 is not "slower", it is unsupported. So int4 via AWQ is the only
thing that makes a 7B fit three ways on 40 GiB. vLLM runs AWQ through Marlin
kernels, which do support sm80.

Why the weights are 5.2 GiB and not the 3.8 GiB that 7.62B × 0.5 bytes suggests:

```
quantized linear layers   6.53B x ~0.52 B (4-bit + fp16 scales/zeros, g=128)  ~3.16 GiB
embed_tokens              152,064 x 3,584 x 2 B, NOT quantized                ~1.02 GiB
lm_head                   152,064 x 3,584 x 2 B, NOT quantized, NOT tied      ~1.02 GiB
                                                                              ---------
                                                                               ~5.20 GiB
```

**AWQ does not quantize the embedding or the output head, and Qwen2.5-7B does not
tie them** — so they are counted twice, at fp16. That is 2.03 GiB, **39% of the
total weight footprint**, and omitting it is how a capacity estimate comes out
optimistic by a third.

**The KV cache stays fp16.** `kv_cache_dtype=fp8` would halve bytes/token and
roughly double concurrency — and on sm80 it would not work anyway. But the reason
not to reach for it even on a card that could: how fast the cache fills is the
thing we are studying.

Worth being explicit about the inversion here. On a larger card we would have
*declined* to quantize, because freeing memory triples concurrency and leaves
nothing for admission to bind on. On this card quantization is the price of
entry. **Same technique, opposite verdict, because the constraint moved.**

## 5 · Parallelism: TP=1

5.2 GiB of weights fits a 12 GiB slice, so there is nothing to shard. No tensor
parallelism, no all-reduce, each worker owns its slice.

TP>1 across HAMi slices would be actively wrong, and the reason is worth stating:
**slices of one card are not independent HBM pools.** A TP=2 worker spanning two
slices of the same card would do all-reduce against itself over the same memory,
pay the collective's cost, and gain no capacity. It would also look like a
two-GPU deployment in every log.

## 6 · Replicas and topology: 1 prefill + 2 decode

The brief's floor is **at least two workers**, and its options are explicitly
"two/three colocated replicas, or prefill/decode split."

| Topology | slices | KV/replica | seq @16k | pool | hop? | placement? |
|---|---|---|---|---|---|---|
| **A** 3 colocated | 3 × 12 GiB | 4.80 | 5.5 | 16.5 | **no** | yes |
| **B** 1 prefill + 2 decode | 3 × 12 GiB | 4.80 | 5.5 | **11.0** | **yes** | **yes** |
| **C** 2 prefill + 2 decode | 4 × 9 GiB | 2.10 | 2.4 | 4.8 | yes | yes |
| **D** 1 prefill + 1 decode | 2 × 18 GiB | 10.00 | 11.4 | 11.4 | yes | **no** |

**B.** It is the cheapest shape where all six of the brief's decisions are real:
`src != dst` always, so the hop is never a no-op; two decode candidates, so
`pick` is a genuine decision; and KV still binds at a pool of 11.

The rejections:

- **A** gives the most concurrency and no hop at all — `src == dst` always. Part 6
  would become a warmup-only proof, which the brief permits ("hop-**or**-warmup")
  but which gives up Mooncake and the transfer measurement.
- **C** halves the KV per replica to 2.10 GiB, and over-provisions the thing that
  needs it least: a prefill worker builds KV and frees it immediately, so it never
  holds a live sequence. Two prefill workers buy very little and cost 5.2 GiB of
  duplicated weights.
- **D** has one decode worker, so `pick` has no alternatives. That is routing, not
  placement, and Part 4 would have nothing to measure.

### One prefill worker is enough

Because **only the decode pool gates concurrency.** Prefill's 4.80 GiB churns
hard — it builds a 12k prompt's KV, ships it, frees it — but at any instant it
holds far less than a decode worker does, because a decode worker holds its
sequence for the entire generation.

---

## The configuration

**1 × A100-40GB. `Qwen2.5-7B-Instruct-AWQ` at int4. TP=1, three HAMi slices of
12 GiB: one prefill and two decode. `max_model_len` 16,384, `max_num_seqs` 64.**

```
slice  pod         gpumem   gpucores   weights   KV        seq @16k
  0    prefill-0   12 GiB     40%      5.2 GiB  4.80 GiB    5.5
  1    decode-0    12 GiB     28%      5.2 GiB  4.80 GiB    5.5   <- placement
  2    decode-1    12 GiB     28%      5.2 GiB  4.80 GiB    5.5   <- placement
                   -------    ----
                   36 GiB      96%        decode pool = 11.0 concurrent
```

## The arithmetic

```
kv_bytes_per_token = 2 x 28 x 4 x 128 x 2  =  57,344 B  =  56 KiB

per slice   = 12 GiB x 0.90  =  10.80 GiB budget      <- 0.90 of the SLICE
            - weights         =   5.20 GiB
            - activations     =   0.80 GiB
                                 ----------
            KV                =   4.80 GiB

one 16k sequence = 56 KiB x 16,384  =  0.875 GiB
concurrency      = 4.80 / 0.875     =  5.5 seq/replica
decode pool (x2)                    =  11.0 concurrent
```

**`gpu_memory_utilization` applies to the slice, not the card.** HAMi gives the
pod a hard `gpumem` cap that it sees as its entire device, and vLLM's 0.90 then
applies to that. Reading the 0.90 against the 40 GiB card gives 33.3 GiB and a
KV figure **3× too high**.

### At `max_len` and at the length the app actually sends

The brief asks for both. They differ by 16×.

| Length | Why | GiB/seq | % of a cache | seq/replica | **decode pool** |
|---|---|---|---|---|---|
| 16,384 (`max_len`) | engine ceiling | 0.875 | 18% | 5.5 | **11.0** |
| ~15,400 | turn 4: 1k prefix + 12k source + ~2.4k history | 0.822 | 17% | 5.8 | 11.7 |
| 12,288 | one fetched source alone | 0.656 | 14% | 7.3 | 14.6 |
| ~8,100 | mean across a 4-turn session | 0.433 | 9% | 11.1 | 22.2 |
| ~1,040 | turn 1: prefix + spoken question | 0.056 | 1% | 86.4 | 172.8 |

The fleet serves **11 concurrent sessions in the worst case and ~22 at the mean
request size.** Admission matters in the first regime and not the second, which
is why load level is the x-axis of the experiment in [Part 8](08-proof.md).

### Context length, and why 16,384

| Context | GiB/seq | seq/replica | Note |
|---|---|---|---|
| 32,768 | 1.750 | 2.7 | one request is 36% of a cache — too coarse to schedule |
| **16,384** | **0.875** | **5.5** | **chosen** |
| 8,192 | 0.438 | 11.0 | would force 6k source truncation |
| 2,048 | 0.109 | 43.9 | the lab's setting — nothing ever binds |

16,384 is the smallest ceiling that holds a real turn-4 request (~15.4k) without
truncating sources below 12k, and the largest at which the pool still holds
enough sequences for admission to be a decision rather than a coin flip.

## What replication costs

The central trade, stated as a number:

```
fleet weights  = 3 x 5.20  = 15.60 GiB of the 36 GiB allocated
fleet KV       = 3 x 4.80  = 14.40 GiB
one whole-card worker      = 26.60 GiB of KV

replication costs 12.2 GiB of KV
```

**48% of every slice is a duplicate of the same model.** We pay 12.2 GiB of cache
— and roughly half the concurrency — to have three addressable workers: a
placement decision and a hop. That is the design's central trade and it should be
read as a trade, not a free structural win.

It is also the honest answer to "if cache is full, add a replica": on this card a
replica *removes* 5.2 GiB from fleet KV. Adding one multiplies KV pressure rather
than relieving it.

## Which limiter binds

**KV.** Written down before the run.

| Candidate | Why not |
|---|---|
| weights | 5.20 GiB against a 10.80 GiB budget — comfortable |
| **KV** | **4.80 / 0.875 = 5.5 sequences. This binds** |
| compute | 5 concurrent sequences is a small batch; 28% of an A100's SMs is ample for it |
| interconnect | TP=1, no all-reduce. The hop stays inside one card |
| scheduler | `max_num_seqs` 64 is ~12× what KV allows |

`max_num_seqs` is deliberately set above the KV limit. If it were below it, the
scheduler would bind first and **a KV-pressure policy could never fire** — the
cache would never reach 0.90 because the scheduler would stop admitting
sequences before the blocks ran out. That is the `half-capacity-headroom` failure
in one sentence, and it is why the renderer should print which resource binds
rather than leaving it to be worked out:

```
~5.5 seqs at 16384 tokens (4.80 GiB KV), max_num_seqs=64 -> KV binds — admission can bite
```

## Measured — stage 0, 2026-10-10

One worker on the whole card, `--gpu-memory-utilization 0.90`. Card:
**NVIDIA A100-SXM4-40GB**, sm80, 40 GiB. vLLM **0.31.0**, torch 2.13.0+cu130.

| | Predicted | **Measured** | |
|---|---|---|---|
| `kv_bytes_per_token` | 56 KiB | **56.0 KiB** | **P6 held.** 28 layers × 4 KV heads × head_dim 128, and the engine's own `539,808 tokens = 28.83 GiB` agrees independently |
| weights + activations + CUDA graphs | 6.00 GiB | **7.17 GiB** | **+1.17 GiB, +20%** — the one that was wrong |
| KV on the whole card | — | 28.83 GiB | 36.00 budget − 7.17 overhead |

**The overhead miss is the finding.** 5.20 GiB of weights plus 0.80 GiB of
runtime was optimistic: vLLM 0.31 also reserves CUDA-graph memory, and that was
not in the estimate. It propagates straight into the slice arithmetic:

```
                        predicted           measured
12 GiB slice x 0.90      10.80 GiB budget    10.80 GiB
  - overhead             -6.00               -7.17
  = KV per worker         4.80 GiB            3.63 GiB
  / 0.875 GiB per 16k seq
  = seq per replica       5.5                 4.1
  decode pool (x2)       11.0                 8.3
```

**The configuration does not change.** A decode pool of 8.3 against 14
concurrent sessions still means KV binds and admission can fire — the premise
survives, with less headroom than claimed. Three 13 GiB slices would recover the
pool to 10.4, but 3 × 13 = 39 GiB exceeds the ~37 GiB allocatable, so 12 GiB
stands.

Stage 2 re-measures this per slice. CUDA-graph reservation may not scale down
linearly with the slice, so 7.17 GiB is an upper bound until then.

## Predictions, written before the run

Checked in [Part 8](08-proof.md) and **not edited afterwards**.

| # | Prediction | How it gets checked |
|---|---|---|
| P1 | **KV binds, not the scheduler** | `kv_cache_usage_perc` saturates *before* `num_requests_waiting` grows |
| P2 | **Signal reaches 0.90 at 14 sessions** (past the 11.0 pool) | `admitperf report` shows `max >= 0.90`, liveness `PASSED` |
| P3 | **Signal does not reach 0.90 at 2 sessions** (~18% of pool) | same report at the low-load run shows `max < 0.90` |
| P4 | Prefix-cache hit rate **> 70%** — 1k of system prompt and tool schemas is shared across every turn | `prefix_cache_hit_rate` |
| P5 | Traffic is **prefill-heavy, > 10:1** prompt to completion | mean prompt ÷ mean completion from the decision log |
| P6 | `kv_bytes_per_token` = **56 KiB ±5%** | ✅ **HELD** — 56.0 KiB, measured 2026-10-10 |
| P7 | Inter-token p95 on decode **spikes during a large prefill**, because `gpucores` is time-sliced rather than partitioned | per-pod inter-token latency against prefill activity |
| P8 | Mooncake hop is **far above our 150 ms first-principles estimate**, nearer the 0.5–0.8 s published prior | stage 8b, measured |

P2 and P3 together are the experiment. P6 comes first in time, because every
figure on this page moves with it. P8 is a prediction *against* our own
arithmetic, recorded because a published single-GPU measurement disagrees with
it — see [Part 6](06-hop.md).
