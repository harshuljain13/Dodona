# Requirements: Dodona inference experiments

## Problem Statement

Dodona's cluster runs, but **not one of its design choices has been justified by
a measurement.** The documents claim benefits for prefill/decode
disaggregation, prefix-aware routing, admission control and the KV hop; zero
A/B comparisons have been run, so every claim is an assertion. Worse, three
components are simulated while being described as real — the KV hop records a
transfer that never moves bytes, KV eviction operates on a toy cache, and the
warm-gate speedup is a formula's output presented as a measurement.

For a cohort submission and an ODSC workshop, that is the whole deliverable
missing. Other submissions in the same cohort landed complete result sets —
including honest negative results — on far smaller GPUs, because they measured.

## Goals

1. **Every design choice in the submission is backed by a run**, with the
   component as the only variable and a named metric that moves.
2. **Real prefill/decode disaggregation**: KV blocks physically move between
   pods, proved by decode TTFT on a long prompt, not by a JSON record.
3. **Reproducible, comparable runs** — same workload, same seed, one variable
   per arm. A run that cannot be repeated is not evidence.
4. **No simulated component anywhere in a measured path.** If something is
   simulated it is excluded from the arm or the arm is labelled as such.
5. **Each result explained**, including the negatives. "The policy was inert
   and here is the signal proving it" is a result.

## Non-Goals

- Changing the model. Qwen2.5-7B-Instruct-AWQ stays. Scarcity is created with
  `--max-num-seqs`, not by swapping weights.
- llm-d, Envoy Gateway, or the Gateway API Inference Extension. Our own gateway
  is deployed and verified in the request path.
- Multi-node, multi-GPU, or tensor parallelism. One card, three HAMi slices.
- Voice (Deepgram) in the measured path. Speech is a network constant and not
  what we are measuring.
- Autoscaling as an experiment. KEDA stays wired at `min 1 · max 2` so the
  mechanism is visible, but the fleet is fixed during any measured run.

## Success Criteria

| | Criterion | Evidence |
|---|---|---|
| S1 | The KV hop moves real blocks | decode TTFT on a ~12k prompt drops sharply with the connector vs without |
| S2 | Disaggregation is justified or rejected | split vs colocated, on the same workload, with the decision stated either way |
| S3 | The second decode replica is justified | 1 decode vs 2 decode, on tail latency and completion count |
| S4 | Admission control's effect is known | policy on vs off: preemptions, shed count, p95 |
| S5 | Routing policy's effect is known | `prefix_then_load` vs `least_loaded`: prefix cache hit rate and TTFT |
| S6 | Every run is reproducible | same `--seed` reproduces the arm; bundles under `metrics/<run>/` |
| S7 | What binds is measured, not assumed | the limiter is named with the signal that proves it |

A negative or neutral result satisfies these criteria. An unrun experiment does
not.

## Constraints

- **1 × NVIDIA H100 PCIe 80 GiB**, sm90, driver 580.105.08, CUDA 12.8, single
  k3s node. ~79.6 GiB usable.
- **vLLM 0.31.0.** Registered connectors include `MooncakeConnector`,
  `MooncakeStoreConnector`, `LMCacheConnectorV1`, `LMCacheMPConnector`,
  `NixlConnector`, `ExampleConnector`. **None of the `mooncake`, `lmcache` or
  `nixl` Python packages are installed**, in the venv or (unverified) in the
  `vllm/vllm-openai` image.
- **RDMA is present** — `mlx5_0`, InfiniBand transport, `/dev/infiniband/uverbs0`.
  Unusual for a single-node rental and relevant: a peer submission measured
  NIXL over TCP at ~4 s versus LMCache at 0.5–0.8 s.
- **KV does not bind on this card.** Measured: 64.32 GiB of KV on the whole
  card = 1,204,384 tokens = 73.5 concurrent 16k sequences. A `kv_threshold`
  policy has nothing to decide, which is itself a finding.
- GPU time is rented and metered. Arms must be short and scripted, not
  interactive.
- Three components are currently simulated: `kvbus.transfer()`, `kv_eviction`,
  `warmup`. The first is in scope to make real; the other two are not.

## Open Questions

| | Question | How it gets answered | Blocks |
|---|---|---|---|
| Q1 | Can a KV connector move blocks between two **HAMi slices of the same physical GPU**? Nobody in the lab lineage has done this, and HAMi intercepts CUDA calls to enforce its cap. | Deploy two pods with `--kv-transfer-config` and read the engine log for connector init, then measure decode TTFT. | E1, and therefore S1 |
| Q2 | Does `vllm/vllm-openai:latest` ship `nixl` / `lmcache` / `mooncake`? | `docker run --rm vllm/vllm-openai:latest python -c "import nixl"` — needs `sudo` on this box. | Q1 |
| Q3 | Which connector works here: `MooncakeStoreConnector`, `LMCacheConnectorV1`, or `ExampleConnector` (filesystem)? | Try in that order; `ExampleConnector` over a `hostPath` is the fallback with no extra dependency. | E1 |
| Q4 | With `--max-num-seqs 8`, does `num_requests_waiting` actually rise under our agent-shaped load? | E0, before any policy arm runs. | E3, E4 |
| Q5 | Is `prefix_tokens` surfaceable to the router as a usable heuristic? Against real vLLM it is `None`, so the prefix scorer is inert. | Surface `bus.prefixes_for(self.id)` and measure `prefix_cache_hits/queries`. | E5 |

## Prior art worth copying

- **A frozen, replayable workload** is what makes pass rates comparable. One
  peer submission replays 26 recorded faults over identical evidence; ours is
  `load.py --seed`, which is deterministic but synthetic.
- **Cap the scheduler to create scarcity.** `--max-num-seqs 8` made decode
  slots the limiter for a peer on a 0.6B. It is the reason an admission policy
  has anything to decide on a card with 64 GiB of free KV.
- **Report the negatives.** "Admission control was neutral-to-negative because
  the GPU was never overwhelmed" is a stronger result than a tuned win.
