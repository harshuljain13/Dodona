# Runbook — bringing Dodona up, stage by stage

**Each stage ends with something testable, and the next does not start until it
does.** Nothing here is inherited as a black box; the class-10 lab is the
reference you port from, piece by piece.

Target: **1 × A100-40GB**, `Qwen2.5-7B-Instruct-AWQ`, three HAMi slices of
12 GiB, one prefill + two decode.

**[`COMMANDS.md`](COMMANDS.md) is the copy-pasteable version of this file.** Use that
to run; use this to understand what you are checking and why.

Stages 0–1 need no Kubernetes. Stage 3 before stage 4 is deliberate: admission is
a *function of* engine metrics, so the scrape must exist before there is anything
to decide on.

---

## Stage 0 — one model, on the bare box

No k8s, no slices, no gateway. Docker and one vLLM container with the whole card.

```
nvidia-smi                                    # driver, 40960 MiB, sm80
docker run --gpus all -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --quantization awq_marlin \
  --max-model-len 16384 \
  --max-num-seqs 64 \
  --gpu-memory-utilization 0.90 \
  --enable-prefix-caching \
  --enable-auto-tool-choice --tool-call-parser hermes
```

**Gate — all four, before anything else is built:**

| | Check | Expect |
|---|---|---|
| 0a | `curl localhost:8000/v1/chat/completions -d '{...}'` | 200, text back |
| 0b | `curl localhost:8000/metrics \| grep kv_cache` | `vllm:gpu_cache_usage_perc` present |
| 0c | **`config.json`: layers, `num_key_value_heads`, `head_dim`** | 28 / 4 / 128 → **56 KiB/token**. This is prediction **P6** |
| 0d | `nvidia-smi` memory used vs `5.2 GiB` weights estimate | within ~10% |

**0c and 0d are the whole point of this stage.** Every concurrency figure in
Part 1 is derived from them. If `head_dim` is not 128, or weights are not ~5.2
GiB, stop and redo the arithmetic before renting more time.

Also worth timing here while the card is uncontended: a 12k-token prefill, so
Part 6's recompute-vs-hop comparison has a measured left-hand side.

---

## Stage 1 — Open WebUI, for hand testing

```
docker run -p 3000:8080 \
  -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 \
  -e OPENAI_API_KEY=dummy \
  ghcr.io/open-webui/open-webui:main
```

**Gate:** you talk to it in a browser and it answers.

Why before the agent: a broken agent and a broken gateway produce the same
symptom. Establishing that the *engine* is good by hand means every later failure
has a smaller search space.

---

## Stage 2 — k3s + HAMi, three slices

```
# k3s with the NVIDIA container runtime as default
curl -sfL https://get.k3s.io | sh -
# HAMi via helm
helm repo add hami-charts https://project-hami.github.io/HAMi/
helm upgrade --install hami hami-charts/hami -n kube-system
```

Three pods, one prefill and two decode, each:

```yaml
resources:
  limits:
    nvidia.com/gpu: "1"
    nvidia.com/gpumem: "12288"      # 12 GiB — the pod sees this as its whole device
    nvidia.com/gpucores: "40"       # prefill 40, decode 28 each -> 96%
```

**Apply them one at a time, waiting for each to serve before the next.** Pods
contending for memory at startup can leave one with half the KV of its peers, and
a fleet with unequal caches makes the pool-wide concurrency figure wrong.

**Gate — the integrity check. The run is void without it:**

```
kubectl get pods -o wide                                     # 3 vllm pods, Running
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
#  -> 3 processes on index 0, each ~10-11 GiB, NONE above its slice
```

The failure mode here is the inverse of a multi-card fleet's. There, the danger
is two pods landing on one card. Here it is **one pod exceeding its slice and
quietly taking memory from another** — so what you are checking is that HAMi is
enforcing the cap.

Record the as-deployed table now: per-pod GPU index, memory, KV cache tokens. If
the KV token counts differ between the two decode pods, restart rolling.

---

## Stage 3 — Prometheus + Grafana

```
helm upgrade --install kube-prom prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace
# ServiceMonitor or scrape annotations on all three vllm pods
```

**Gate: a live `/metrics` scrape, per pod, visible in Grafana.** This is one of
the brief's two pass/fail items — *"a design without a live `/metrics` (or
Grafana) scrape and a hop-or-warmup proof fails."*

Minimum panels before moving on: KV usage per pod, `num_requests_waiting` per
pod, `num_requests_running`, `num_preemptions`, `prefix_cache_hit_rate`.

Save the first scrape to `../metrics/stage3/` — it is the baseline everything
later is compared against.

---

## Stage 4 — the gateway, pass-through only

A single process on `:8080` that forwards `/v1/chat/completions` to one pod and
nothing else. No guard, no admission, no placement.

Repoint Open WebUI at `:8080`.

**Gate:** traffic flows through our box and still answers. Nothing is decided
yet, and that is the point — this stage proves the plumbing in isolation.

Two things to get right here because they are invisible later:

- **The worker payload is an allowlist.** `tools`, `tool_choice`, `temperature`,
  `top_p`, `stop` cross; `kv_hop`, `prefix_hash`, `tenant`, `priority` do not.
  Test it now: send `tools`, confirm the worker receives `tools`.
- **Do not synthesise SSE and then quote client-side TTFT.** If the gateway
  returns a completed response as a fake stream, client TTFT measures the whole
  generation. TTFT comes from vLLM's metrics, always.

---

## Stage 5 — guard and the tenant window

```
inspect(payload) → Guard                       # model allowlist, token cap, shape
tenant window                                  # token budget per caller
```

**Gate:** a malformed request gets 400 and a tenant over quota gets 429, and
**neither touches a GPU**.

The ordering is load-bearing: the tenant window runs **before** the capacity
question, because a 429 is client-attributable and must never reach the paid
overflow provider. "A 429 that overflowed" is on the brief's bad-answer list.

---

## Stage 6 — admission, as a socket

Install AdmitPerf in the request path. Scrape the three pods, hand the metrics
plus the request to the policy, do what comes back, write one row.

```
ADMISSION_POLICY=kv_threshold
ADMISSION_THRESHOLD=0.90
ADMISSION_LOG=../metrics/<run>/decisions.jsonl
```

**Gate — three things, all of them:**

| | Check |
|---|---|
| 6a | `decisions.jsonl` has one row per request, each carrying **the signal value the policy read** |
| 6b | `admitperf_gate_active 1` and `admitperf_gate_recording 1` |
| 6c | `admitperf report <log>` prints a page with a liveness verdict on line one |

Then run `ADMISSION_POLICY=no_admission` and confirm it produces a log of the
same shape. **That is the baseline** — with the gate switched off entirely there
would be nothing to compare against.

Verify the fail-safe before trusting any later number: break the scrape
deliberately and confirm the metric is **omitted, not zeroed**.
`kv_cache_usage: 0.0` reads as a completely empty cache and would make every
policy admit — a failed scrape must not silently switch admission off.

---

## Stage 7 — placement

```
pick(req, workers, *, policy) → Worker | Shed
```

Two decode candidates, so this is a real decision. Different scorers per pool:

| Pool | Scorers, in order |
|---|---|
| prefill | prefix-cache locality → uncached prefill tokens → KV headroom |
| decode | active requests → **queue depth** → KV headroom |

Queue depth must be a **scorer**, not only an admit input — the brief asks for
this explicitly.

**Gate:** under load, requests spread across both decode pods **by score**, not
round-robin, and the per-pod panel shows it. **Do not bounce** — a `Shed` is a
refusal with a reason, not a re-pick on another worker.

---

## Stage 8 — the hop, and warmth

```
src == dst  ->  nothing to do (cannot happen in this topology)
src != dst  ->  record: from prefill-0, to decode-{0,1}, N tokens, prefix, backend
```

`KV_BACKEND=mooncake`. **Check this before drawing any conclusion from an empty
`/hops` panel** — `nccl.py` and `nixl.py` return immediately without moving
bytes, and "NCCL/NIXL in this repo moves KV tensors" is on the bad-answer list.

**Gate — the other half of the pass/fail:**

| | Check |
|---|---|
| 8a | A recorded hop at `/hops`: count, tokens, bytes |
| 8b | Measured transfer time for a 12k prompt (0.656 GiB) — this settles **P8** |
| 8c | **Cold vs warm TTFT, re-quoted.** Hop onto a freshly started decode replica, measure the first token, warm it, measure again |

8c is the warmth proof. `READY 1/1` appears minutes before the engine can
actually serve, so a TTFT off a cold replica is meaningless — and in a voice turn
it is audible as dead air.

---

## Stage 9 — the agent

Point the Dodona agent at `:8080`. Every completion goes through the serve path;
completions that skip guard/admit/place/queue do not count.

**Gate, in order** — each step fails for a different reason, so running them in
sequence localises a break:

| | Check | Passes when |
|---|---|---|
| 9a | `curl` the gateway with a `tools` array | response has `tool_calls`, not prose |
| 9b | agent, one question, one turn | `web_search` fires |
| 9c | agent, full question | multi-turn, cites a URL it actually fetched |
| 9d | voice loop end to end | spoken question in, spoken cited answer out |
| 9e | decision log | one row per completion, gate recording |

9a is the one that fails if the allowlist from stage 4 regressed.

---

## Stage 10 — measure

| Run | Policy | Sessions | Seeds |
|---|---|---|---|
| `low-baseline` | `no_admission` | 2 | 3 |
| `low` | `kv_threshold` 0.90 | 2 | 3 |
| `high-baseline` | `no_admission` | 14 | 3 |
| `high` | `kv_threshold` 0.90 | 14 | 3 |

Back to back, same deploy, seed varied between repeats. **The fleet must not
change size between runs in a comparison** — a resized fleet means the deployment
moved, not the policy.

Then: `admitperf report` per run, `admitperf compare` baseline against policy,
Grafana exhibits, and check predictions P1–P8 in
[`SUBMISSION.md`](SUBMISSION.md#predictions-written-before-the-run)
**without editing them**.

---

## If time runs short

Cut from the top of this list, in this order. Do not reorder it in the moment.

1. Experiment 2 (prefix-mix comparison) — **whole, not half**
2. Seeds 2 and 3 at the **low** level — the crossing is at the high level
3. The Grafana exhibits
4. Stage 9d, the voice loop — text through the gateway still proves the serve path

**Never cut:** stage 2's integrity check, stage 3's scrape, stage 8's hop-or-warm
proof, or the baselines. The first two are pass/fail; a run without a baseline
has a one-sided comparison; and a fleet that was secretly two workers invalidates
everything silently.
