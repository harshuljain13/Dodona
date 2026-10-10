# Learnings

What we got wrong on the way, and what each thing taught. Written as it
happened, not reconstructed afterwards.

[`SUBMISSION.md`](SUBMISSION.md#deployment-issues) has the terse version —
symptom, cause, fix — because a submission needs it short. This is the long
version: what we *believed*, what actually happened, and what it changes.

The entries worth reading first are
[#1](#1-naming-a-thing-is-not-building-it) and
[#11](#11-agent-traffic-does-not-saturate-like-a-benchmark). The first is the
largest gap between what the documents claim and what runs; the second decides
whether the experiment the project is built around can produce a result at all.

---

## Capacity and arithmetic

### 1. Naming a thing is not building it

**Believed:** the fleet is `1 prefill + 2 decode`, disaggregated, with a KV hop
between the pools. Part 2 says so, the README diagram draws it, Part 6 computes
the hop's economics.

**Actually:** the three pods were started with **identical arguments** and no
`--kv-transfer-config`. There is no connector, no KV transport, no proxy. They
are three complete, independent vLLM replicas. `vllm-prefill` had served
**zero** requests — `prompt_tokens_total = 0` — while the decode pods had done
410,043 prompt tokens between them.

So the running system is **topology A wearing topology B's labels**, and a
quarter of the GPU was idle holding weights for a pod nothing routed to.

**What it teaches.** The label in a manifest is not a mechanism. Real
disaggregation needs three things we did not have: `--kv-transfer-config` naming
a connector on both sides, a transport the two share, and a proxy that sends the
prompt to prefill and then hands the request to decode. "Mooncake is not
deployed" sounded like a cosmetic gap in stage 8. It was the entire mechanism.

**What it changes.** Either the documents get corrected to topology A and Part 6
is satisfied by the warmup proof — the brief says "hop-**or**-warmup", so that
is legitimate — or disaggregation gets built for real. What is not acceptable is
a diagram that draws a transfer which never happens.

**How to not repeat it:** for any claim about structure, find the artifact that
proves it. Here one `kubectl get deploy -o jsonpath='{...args}'` would have
settled it on day one, and a metric as blunt as `prompt_tokens_total = 0` was
sitting there the whole time.

### 2. Overhead is not a constant, and the "correction" was worse than the estimate

Three numbers for the same quantity:

| | overhead | KV/slice | decode pool |
|---|---|---|---|
| predicted on paper | 6.00 GiB | 4.80 | 11.0 |
| extrapolated from stage 0 | 7.17 GiB | 3.63 | 8.3 |
| **measured at stage 2** | **6.53 GiB** | **4.27** | **9.8** |

**Believed:** weights plus activations is roughly fixed, so measuring it once on
the whole card tells you what a slice will have.

**Actually:** vLLM also reserves CUDA-graph memory, and that **scales with the
memory available to it**. On the whole card the overhead was 7.17 GiB; on a
12 GiB slice it was 6.53. Treating the whole-card figure as constant
under-predicted the pool by 18% — and the original paper estimate, which I had
"corrected", turned out closer than the correction.

**What it teaches.** Measure on the configuration you will run. An extrapolation
from a different one is a third number, not a better one. `gate.py` now refuses
to extrapolate from the whole card to a slice and points at the pods instead.

### 3. Read `head_dim`, never derive it

`kv_bytes_per_token = 2 × layers × kv_heads × head_dim × dtype_bytes` is the
foundation of every concurrency figure in the project. The trap is `head_dim`:
Qwen3 **declares** it as 128 while `hidden/heads` = 2560/32 = **80**. Deriving
it the usual way is wrong by 60%, silently, and every downstream number inherits
the error.

P6 held for us — 28 layers × 4 KV heads × head_dim 128 = 56 KiB/token, confirmed
three ways: from `config.json`, from vLLM's own `539,808 tokens = 28.83 GiB`,
and again per-slice at `80,032 tokens = 4.27 GiB`.

**What it teaches.** A number with three independent confirmations is worth the
five minutes. The check runs first, before anything else is believed.

### 4. Parameter count does not predict cache cost

`Qwen3-4B` is 47% smaller than `Qwen2.5-7B` and costs **2.6× more** per token —
144 KiB against 56 — because it has twice the KV heads and eight more layers.
Quantization changes *weights*; only architecture changes *bytes per token*.

This is why the model choice was not "pick the biggest that fits".

### 5. "Loads successfully" and "can serve" are different states

7B at bf16 in an 18 GiB slice loads: 14.19 GiB of weights against a 16.20 GiB
budget. The pod reports `READY 1/1`. It then has 0.81 GiB of KV, one 16k
sequence needs 0.875, and **every request is refused**.

A config check that compares weights against available memory lets this straight
through. It has to require room for at least one sequence at `max_model_len`.

---

## Kubernetes and the GPU

### 6. A full GPU cannot do a RollingUpdate

Adding two flags to the fleet hung the rollout. Both new pods sat `Pending`
forever.

Kubernetes' default strategy **surges a replacement before retiring the old
pod**. With all 36 GiB allocated, the replacement can never be scheduled, so it
waits — and the rollout waits with it, indefinitely.

`strategy: { type: Recreate }`. **A single card cannot be rolled without
downtime**, and the default pretends otherwise by deadlocking rather than
failing.

### 7. ClusterIP is invisible from the host

Hours of "I can't reach anything". The Services were `ClusterIP`, which exists
only inside the cluster network — so there was nothing on the host for the ssh
tunnel to forward. The pods had been serving correctly the entire time.

NodePort, and the tunnel forwards the NodePort. Equally: a helm chart that picks
a **random** NodePort (Grafana landed on 31457) against a tunnel with a fixed
port list means the service is healthy and simply unreachable, with nothing in
any log to say why. Pin it.

### 8. Empty dashboards can be correct

Most Grafana panels showed "No data", which looked like a broken observability
stack. It was not:

| prefix | names in Prometheus | |
|---|---|---|
| `vllm:` | 102 | ✅ |
| `kube_` · `node_` · `container_` · `DCGM_FI` | 586 | ✅ |
| `orch_` | **0** | the gateway does not exist yet |
| `mooncake` | **0** | never deployed |

The class-10 dashboards are built almost entirely on `orch_*` — 16 of their top
20 queried metrics — and the gateway that emits them is stage 4. **The panels
were accurately reporting a half-built system.**

**What it teaches.** Before debugging a dashboard, ask what emits the metric and
whether that thing exists. `cluster/setup/report.py --dashboards` now answers
this directly rather than leaving it to be rediscovered.

### 9. vLLM V1 renamed the metric admission reads

`gpu_cache_usage_perc` → **`kv_cache_usage_perc`**. The old name does not exist.

Eight documents asserted a metric that is gone. The control plane happened to
survive because `control/router/pools.py` already accepted both names — luck
inherited from the lab, not foresight.

**What it teaches.** A metric name is an interface. Pin the engine version, or
accept both names, or check at startup — but do not assume a gauge is still
called what it was called.

### 10. Tool-calling flags are not optional, even with no tools

`--enable-auto-tool-choice` and `--tool-call-parser` were deferred to stage 9 on
the reasoning that stage 0 has no tools and Open WebUI does not use them.

The second half was wrong. **Every OpenAI-compatible client sends
`tool_choice: "auto"` by default**, and vLLM rejects the entire request when it
cannot honour it. Open WebUI was unusable until both flags were on the fleet.

Verified afterwards: `get_weather {"city": "Delhi"}`, `finish_reason:
tool_calls`. Structured, not prose in `message.content` — which is the failure
that would have quietly turned a research agent into a single-turn chatbot.

---

## Load, and the measurement itself

### 11. Agent traffic does not saturate like a benchmark

This is the one that may decide whether the project's central experiment
produces a result.

| sessions | `max_tokens` | gap between turns | peak `kv_cache_usage_perc` |
|---|---|---|---|
| 8 | 150 | 0.5 s | **0.33** |
| 32 | 400 | 0 | **0.54** |

The decode pool holds 9.8 concurrent sequences. Thirty-two sessions — more than
three times that — peaked at 54%.

**Why.** An agent session is four *sequential* turns with tool calls between
them. A session is only in flight during a turn, and it **releases its KV
between turns**. Offered concurrency and resident concurrency are not the same
number, and for this workload they are far apart.

**What it changes.** The planned "high load" of 14 sessions would have peaked
near 0.35 and the `kv_threshold` policy would have been **inert** — installed,
running on every request, unable to change a single outcome. Part 8's load
levels have to be derived from a measured saturation curve, not from the pool
size.

**What it teaches.** The scarcity a policy needs is not the scarcity a capacity
calculation predicts. Finding this at stage 3 rather than stage 10 is worth more
than the load test cost.

### 12. Traffic shape moves with the knobs you were not thinking about

Prompt-to-completion ratio across the same workload: **34.7:1** at
`max_tokens=150`, **14.0:1** at 400. Same prompts, same sessions — only the
output cap moved.

P5 predicted "> 10:1" and holds either way, but a ratio quoted without the
settings that produced it is not a measurement of the workload.

---

## Working method

### 13. `bash -n` cannot catch a remote runtime bug

Two scripts shipped that had never been run against a real box:

- `pkill -f 'vllm serve'` over ssh **matches its own command line** — the
  pattern is part of the remote command string, so pkill kills its own shell,
  the connection drops, and ssh returns 255 pointing at nothing useful.
  `pkill -f '[v]llm serve'` fixes it; `pkill -u "$(id -u)"` stops it reaching
  into containers it has no business touching.
- A `syntax error near unexpected token '('` at a line that was fine, because
  the script ran under `sh` and `SSH=(...)` is a bash array.

Both are valid bash. Neither can be caught without executing. **A script that
drives a remote machine has not been tested until it has driven one.**

### 14. A duplicate is not a failure

`make ssh` printed nine `Address already in use` lines and handed back a shell
with no forwards. That reads as a broken tunnel. It was a **second** tunnel; the
first was working the whole time.

`ssh.sh` now closes any tunnel it already holds, and sets
`ExitOnForwardFailure=yes` — silently handing over a shell whose forwards did
not bind is the failure mode that cost the most time here.

### 15. `--only` should mean only

`make observability` ran the entire cluster stage first, because `--only` was
implemented as `>= 2 then >= 3` — "skip 0 and 1" rather than "just this one". A
flag whose name and behaviour disagree will be trusted for its name.

### 16. Scripts that run remotely must be synced before they run

`make gate` was fixed locally and kept checking the wrong thing, because
`--gate` runs the copy **on the box** and that copy was stale. It now rsyncs the
script first.

---

## What is still open

Recorded here so it is not rediscovered.

| | |
|---|---|
| **Disaggregation is not wired** | See [#1](#1-naming-a-thing-is-not-building-it). The documents claim it; the fleet does not do it |
| **Mooncake has no image** | `cluster/k8s/mooncake/mooncake.yaml` names `mooncake-transfer-engine:lab`, a local tag that exists nowhere |
| **`vllm-prefill` is idle** | Holding 10.6 GiB and serving nothing. If we stay colocated, routing to all three raises the pool from 9.8 to ~14.6 |
| **Saturation curve unmeasured** | We know 32 sessions reaches 0.54. We do not know what reaches 0.90, and Part 8 needs it |
| **2 pytest collection errors** | From the module-10 port. Not on the critical path, not yet diagnosed |
