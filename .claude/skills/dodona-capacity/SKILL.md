---
name: dodona-capacity
description: >-
  Redo Dodona's capacity arithmetic when a knob moves — GPU, model, quantization, slice
  size, context length, replica count — and propagate it so no document disagrees with
  another. Use when the hardware changes, when choosing or switching a model, when asked
  how many concurrent requests fit, or when a measured number contradicts a predicted one.
---

# Redoing the capacity arithmetic

`docs/project/01-capacity.md` is **the authority for every number in the project**. Change a
number there, and `SUBMISSION.md`, `PROJECT.md` and `README.md` follow. Never the reverse.

## The formula, and the three places it goes wrong

```
kv_bytes_per_token = 2 (K,V) x layers x kv_heads x head_dim x dtype_bytes
KV per worker      = (gpumem x gpu_memory_utilization) - weights - activations
concurrency        = KV / (kv_bytes_per_token x max_model_len)
```

| Mistake | Consequence |
|---|---|
| Applying `gpu_memory_utilization` to the **card** instead of the **slice** | HAMi gives the pod a hard `gpumem` cap it sees as its whole device. On a 12 GiB slice of a 40 GiB card, reading 0.90 against the card inflates KV by **3×** |
| Deriving `head_dim` as `hidden / heads` | Qwen3 declares `head_dim: 128` while `2560/32 = 80`. Off by 60%. **Read `config.json`** |
| Forgetting that quantization spares the embeddings | AWQ leaves `embed_tokens` and `lm_head` in fp16, and Qwen2.5-7B does not tie them — 2.03 GiB, **39% of the weight footprint** |

## Two facts that are easy to get backwards

**Parameter count does not predict cache cost.** `Qwen3-4B` is 47% smaller than
`Qwen2.5-7B` and costs 2.6× more per token (144 KiB vs 56), because of twice the KV heads
and eight more layers. Always compute; never infer from size.

**Quantization changes weights, not bytes per token.** `kv_cache_dtype` is a separate
setting. Only *architecture* moves bytes/token — a model with interleaved local/global
attention bounds most layers to a fixed window, so its growing cost per token can be lower
than a much smaller dense model's.

## Always report two lengths

The brief asks for `max_len` **and** the length the app actually sends, and they differ by
16× here. One number is not an answer.

| Length | Dodona |
|---|---|
| `max_model_len` 16,384 | pool 11.0 |
| mean real request ~8,100 | pool 22.2 |

## Then name the limiter, before the run

Weights, KV, compute, interconnect, or the scheduler — pick one and write it down. The
check that it held: **does `gpu_cache_usage_perc` saturate before `num_requests_waiting`
grows?**

Two traps worth stating explicitly:

- **`max_num_seqs` below the KV limit means a KV policy can never fire.** The scheduler
  stops admitting before the blocks run out, so the cache never reaches the threshold. Keep
  it comfortably above.
- **A fleet where nothing is ever scarce has no admission decision to measure.** If a knob
  change pushes the pool into the dozens, that is a reason to reject the change, not a win.
  Freeing memory is not automatically good here.

## Replication is the hidden cost

```
fleet KV = replicas x ((gpumem x 0.90) - weights - activations)
one whole-card worker KV = (allocatable x 0.90) - weights - activations
```

On Dodona: three copies of the weights take 15.6 of the 36 GiB allocated, leaving 14.4 GiB
of KV where one worker would have had 26.6. **Replication costs 12.2 GiB of KV** to buy a
placement decision and a hop.

This is also the honest answer to "the cache is full, add a replica": a replica *removes*
memory from fleet KV. It multiplies pressure rather than relieving it.

## Propagating a change

In this order, or the documents will disagree:

1. `docs/project/01-capacity.md` — the arithmetic, the decision chain, the predictions
2. `SUBMISSION.md` — Part 1, Part 2's config block, Part 8's session counts
3. `PROJECT.md` — the configuration section and the C4 labels
4. `README.md` — the config block, the badges and the status table
5. `RUNBOOK.md` — stage 0's expected values and stage 2's slice sizes
6. `docs/README.md` — update which deep dives are now stale

**Record the old prediction rather than overwriting it.** A prediction that was wrong by 2×
tells the reader which piece of arithmetic was misunderstood; one silently retrofitted
teaches nothing.
