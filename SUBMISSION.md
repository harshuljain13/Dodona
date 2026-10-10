# Submission — design the cluster and serve an app

Answers to the [project instructions](https://www.notion.so/hj1393/Cohort-Week-10-Final-Project-Instructions-3e9e2e4aacb48061ab1ed3930fc2813a),
part by part. **This is the go-to file.** Every part answers the brief in place
and links out.

| Go deeper | |
|---|---|
| [`PROJECT.md`](PROJECT.md) | The project: the problem, the decisions, and the **C4 architecture levels** |
| [`docs/project/`](docs/README.md) | Per-part deep dives: the full arithmetic, the run protocol, the results report |
| [`docs/cluster/`](docs/cluster/README.md) | The serving stack, file by file — gateway, router, KV bus, observability |

## Progress

| Stage | | Measured |
|---|---|---|
| 0 | engine on the whole card | ✅ **2026-10-10** — all five gate checks pass |
| 1 | Open WebUI | ✅ |
| 2 | k3s + HAMi, 1 prefill + 2 decode | ✅ **2026-10-10** — 3 pods, 10,624 MiB each, KV identical across all three |
| 3 | Prometheus + Grafana | ✅ **2026-10-10** — scraping all three pods, `vllm:kv_cache_usage_perc` live. **Pass/fail item met** |
| 4–8 | gateway · guard · admit · place · hop | not started |
| 9–10 | the agent, then the runs | not started |

**As deployed:** NVIDIA A100-SXM4-40GB (sm80, 40 GiB) · vLLM **0.31.0** ·
torch 2.13.0+cu130 · `Qwen/Qwen2.5-7B-Instruct-AWQ`, Marlin int4 kernels.

### Deployment issues

The brief's bad-answer list is about claims nobody can check. This is the
other half: what actually went wrong, recorded as it happened. The long version
— what we believed, what happened, and what it changes — is in
[`LEARNINGS.md`](LEARNINGS.md).

| Symptom | Cause | Fix |
|---|---|---|
| Gate `5b` failed — no `gpu_cache_usage_perc` | **vLLM V1 renamed the metric to `kv_cache_usage_perc`.** The old name does not exist | Renamed in eight documents. `control/router/pools.py` already accepted both, so the control plane was never broken — but every doc asserted a metric that is gone |
| Gate `5d` failed — "no `model weights` line" | vLLM 0.31 does not log one | Parse `Available KV cache memory` instead and derive overhead, which is the number that sizes the slices |
| Overhead 20% higher than predicted | CUDA-graph reservation was not in the estimate | Recorded, not edited. See Part 1 |
| `make engine` died: `Error 255` after setup succeeded | `pkill -f 'vllm serve'` over ssh **matched its own command line**, killed the remote shell, dropped the connection | `pkill -f '[v]llm serve'` |
| `up.sh: line 101: syntax error near '('` | Invoked under `sh`, which cannot do the `SSH=(...)` array | Guard that refuses to run outside bash |
| `make gate` checked the wrong thing after a fix | `--gate` runs the copy **on the box**, and it was stale | `--gate` rsyncs `gate.py` first |
| `make observability` ran the whole cluster stage first | `--only` was `>= 2 then >= 3`, i.e. "skip 0 and 1", not "just this one" | A `case` on the stage |
| Three `Operation not permitted` from `pkill` | Unscoped, so it reached for the vLLM processes **inside the pods**, which run as root | `pkill -u "$(id -u)"` |
| Grafana unreachable at `127.0.0.1:31495` | The chart assigns a **random** NodePort — it landed on 31457, while the tunnel forwards a fixed list | `nodePort: 31495` pinned in `grafana-values.yaml` |
| Nothing on the fleet reachable from the Mac | Services were **ClusterIP**, which exists only inside the cluster network — the tunnel had nothing on the host to forward. The pods were serving correctly all along | `NodePort` 30800 prefill, 30801 decode, 30030 Open WebUI |
| Open WebUI vanished after stage 2 | It was a docker container from stage 1 and the k3s install took it | Now a pod, pointed at the `vllm-decode` Service so hand testing goes through the decode pool |
| `make ssh` printed nine "Address already in use" and gave a shell with no forwards | A **second** tunnel cannot bind ports the first already holds. The first was working the whole time | `ssh.sh` closes any tunnel it already holds, and `ExitOnForwardFailure=yes` so it refuses rather than pretending |
| Open WebUI: *"auto tool choice requires --enable-auto-tool-choice"* | Deferred those flags to stage 9 on the grounds that stage 0 has no tools — but **every OpenAI-compatible client sends `tool_choice: "auto"`**, so the engine rejects the request outright | Both flags on the fleet from the start. Verified: `get_weather {"city": "Delhi"}`, `finish_reason: tool_calls` |
| Rollout hung — new pods `Pending` forever | A full GPU **cannot do a RollingUpdate**. The default surges a replacement before retiring the old pod, and with 36 GiB allocated it can never be scheduled | `strategy: { type: Recreate }`. A single card cannot be rolled without downtime |

---

**Numbers are marked predicted or measured.** Predictions are written down
before the run and checked afterwards. They do not get edited to match.
`TBM` means to-be-measured — a `TBM` left here is a hole we are disclosing.

---

## Summary

**The application.** Dodona is a **voice** research agent for herbs and
supplements, served from a cluster we configure ourselves. You speak a question;
it searches, fetches sources, reads them, and answers aloud with citations. It
works over several turns and calls tools in between. Deepgram does speech-to-text
and text-to-speech **off-GPU**; the GPU serves only the model.

This is hard on a GPU for two reasons that matter later.

**First, context grows.** Every turn resends the whole conversation. One fetched
source is ~12,000 tokens and arrives as a single large prefill — at the
configuration below that holds **0.66 GiB of KV, and a full 16k turn holds
0.88 GiB: 18% of everything a decode worker has.** The cluster is therefore
deciding, more or less constantly, which requests to let in.

**Second, voice puts a real deadline on every request.** One request carries two
different SLOs: **time-to-first-audio** must be under a second — an
acknowledgement, *"let me look that up"* — while **time-to-full-answer** is 10–30
seconds of research. A request that will miss its deadline is worth refusing at
the door rather than admitting and failing slowly.

**The problem.** That is admission control, and every LLM deployment does it
whether or not anyone designed it — usually by accident, when a queue fills up
and everything slows down. The question worth asking is which signal a policy
should watch: KV pressure, queue depth, deadline slack, or a tenant's quota.

**What we do.** Make admission a **plug-in socket** rather than a hardcoded
check, filled by [AdmitPerf](https://github.com/harshuljain13/AdmitPerf): the
policy declares what signal it reads, every decision writes a row carrying the
value it read, and a one-page report at the end of each run says whether that
signal ever reached its threshold. If it never did, the policy was installed and
inert — and that is worth knowing too.

The deliverable is the cluster, the agent in front of it, the control plane, and
one worked report for one policy (`kv_threshold` at 0.90) on real agent traffic.

We are not comparing four policies and declaring a winner. One cluster cannot
support that claim.

### The three pieces

| Piece | Role | What we do to it |
|---|---|---|
| [AgentShip](https://github.com/Agent-Ship/agent-ship) | The app — a research agent, plus `agentship-voice` for the speech loop | Point its `api_base` at our gateway |
| [AdmitPerf](https://github.com/harshuljain13/AdmitPerf) | The policy registry, the per-decision log, the report generator | Install it in the request path as the admission socket |
| The cluster | Three vLLM workers, the KV hop, the dashboards | Built stage by stage, in the shape of the course lab |

### High level

Three boxes: the app, the control plane we write, the engine we configure.
Detailed C4 levels are in [`PROJECT.md`](PROJECT.md#architecture).

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart LR
    USER(["Listener<br/>speaks a question"])

    subgraph OFFGPU["Off-GPU — Deepgram"]
        STT[/"speech → text"/]
        TTS[/"text → speech"/]
    end

    AS[["Dodona agent<br/>search · fetch · read · cite"]]

    subgraph CTRL["control/ — what we write"]
        direction LR
        G[/"guard<br/>malformed → 400"/]
        A{"admit<br/>a socket,"}
        P["place<br/>which worker"]
        Q["queue + hop<br/>declare warm"]
        G --> A --> P --> Q
    end

    AP[["AdmitPerf<br/>policy · decision log"]]
    OVF[["overflow gate<br/>503 / 529 may leave"]]

    subgraph GPUBOX["1 × A100-40GB · three HAMi slices × 12 GiB"]
        direction LR
        PF["vLLM prefill × 1<br/>builds KV, frees it"]
        MC[("Mooncake<br/>KV store")]
        DC["vLLM decode × 2<br/>holds KV while"]
        PF --> MC --> DC
    end

    OBS[("Prometheus<br/>+ Grafana")]
    REP(["run report<br/>SIGNAL LIVENESS"])

    USER --> STT --> AS --> G
    A <-->|"decide(request, state)"| AP
    P --> PF
    Q -.->|"drives"| MC
    DC -->|"tokens"| AS
    AS --> TTS --> USER
    DC --> OVF
    PF -.->|"kv_cache_usage<br/>num_requests_waiting"| A
    DC -.-> A
    PF --> OBS
    DC --> OBS
    AP --> REP

    class USER input
    class AS,OVF accent
    class STT,TTS neutral
    class G,P,Q primary
    class A decision
    class AP highlight
    class PF,DC secondary
    class MC data
    class OBS neutral
    class REP success

    classDef actor    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef input    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef ext      fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef accent   fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef ours     fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef primary  fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef alt      fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef gate     fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef decision fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef engine   fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef secondary fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef store    fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef data     fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef lib      fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef highlight fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef obs      fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef neutral  fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef overflow fill:#2A0F2A,stroke:#E879F9,stroke-width:2px,color:#F5D0FE
    classDef out      fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef success  fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef pick     fill:#06220F,stroke:#4ADE80,stroke-width:3px,color:#BBF7D0
    classDef dead     fill:#2B0F18,stroke:#F87171,stroke-width:2px,color:#FECACA
    classDef note     fill:#0F1218,stroke:#475569,stroke-width:1.5px,color:#9FB0C9
    linkStyle default stroke:#7C8CA6,stroke-width:1.7px
```

Deepgram sits outside the GPU box on purpose: speech is a **network constant**,
so the contention we measure is purely LLM-side.

### The four resources and the six decisions

| Constrained by | Where it shows up |
|---|---|
| **decode slots** | `max_num_seqs` 64 per pod — never the binding limit here |
| **KV blocks** | 4.27 GiB per decode worker (measured) → **the limiter.** [Part 1](#part-1--capacity-on-paper) |
| **hop bandwidth** | prefill → Mooncake → decode, inside one card. [Part 6](#part-6--hop-when-do-you-move-kv-is-the-replica-warm) |
| **warmup time** | `READY` precedes serving by minutes. [Part 6](#part-6--hop-when-do-you-move-kv-is-the-replica-warm) |

| Decision | Part |
|---|---|
| guard · admit · stay vs leave | [3](#part-3--guardrails-admit-stay-vs-leave) |
| place | [4](#part-4--place) |
| queue (hop) · declare warm | [5](#part-5--queue), [6](#part-6--hop-when-do-you-move-kv-is-the-replica-warm) |

---

## The configuration, in one block

**1 × A100-40GB on one Lambda node. `Qwen2.5-7B-Instruct-AWQ` at int4. TP=1,
three workers on three HAMi slices: one prefill and two decode.
`max_model_len` 16,384, `max_num_seqs` 64. Mooncake for the hop. Overflow is the
a smaller model on Superlinked, hard-capped.**

```
one A100-40GB  ·  40.0 GiB reported  ·  ~37 GiB allocatable across 3 pods

AS DEPLOYED, stage 2, 2026-10-10 — all three figures from the engine itself

slice  pod          gpumem   gpucores   used      KV        tokens    seq @16k
  0    prefill-0    12 GiB     40%     10,624Mi  4.27 GiB   80,032      4.88
  1    decode-0     12 GiB     28%     10,624Mi  4.27 GiB   80,032      4.88
  2    decode-1     12 GiB     28%     10,624Mi  4.27 GiB   80,032      4.88
                    -------    ----    --------
                    36 GiB      96%    31,872Mi      decode pool = 9.8

  All three identical — the one-at-a-time start worked, no startup contention.
  vLLM states the last column itself: "Maximum concurrency for 16,384 tokens
  per request: 4.88x".

  predicted             6.00 GiB overhead -> 4.80 GiB KV -> 5.5  seq -> pool 11.0
  stage-0 extrapolated  7.17 GiB          -> 3.63 GiB    -> 4.15 seq -> pool  8.3
  stage-2 measured      6.53 GiB          -> 4.27 GiB    -> 4.88 seq -> pool  9.8
```

One A100-40 is also what a cohort mate or a workshop attendee can actually rent,
so the result is reproducible.

---

## Part 0 — The application: what are you serving?

**Track B — tool-using agent**, with voice at the edge. A user turn starts a
loop: think → tool → observe → answer. Context grows every step. The system
prompt plus tool schemas are a shared prefix; the conversation is not.

**Domain: herbs and supplements — interactions and evidence.** Specific on
purpose. Spoken queries fit the situation (hands busy in a kitchen or a clinic),
citations are the entire point, and the deliverable is *sourced information*, not
a recommendation. The domain is a config — swapping it for patents or case law
changes the prompt and the tool list and nothing else. **The serving problem is
the project.**

### Why Track B, and why voice

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart TD
    Q{"Which track?"}
    A["Track A — RAG<br/>k chunks + system + question"]
    B["Track B — tool agent<br/>think → tool → observe → answer"]
    AB["A + B combined"]

    AX["one-shot: context never grows<br/>cost at arrival = cost at finish"]
    ABX["explicitly extra, not required<br/>two workloads to characterise"]

    V{"Add voice?"}
    VY["yes — Deepgram, off-GPU<br/>gives every request a real deadline"]
    VX["GPU-hosted Whisper/TTS would<br/>eat a slice and add a stage whose"]

    FIN["CHOSEN<br/>Track B · voice at the edge"]

    Q -->|"mostly prefill"| A
    Q -->|"multi-step decode<br/>+ lengthening prefill"| B
    Q -->|"both"| AB
    A -.-|"rejected"| AX
    AB -.-|"deferred"| ABX
    B ==> V
    V ==> VY ==> FIN
    V -.-|"rejected"| VX

    class B,V,VY,FIN pick
    class Q gate
    class A,AB dead
    class AX,ABX,VX note

    classDef actor    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef input    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef ext      fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef accent   fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef ours     fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef primary  fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef alt      fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef gate     fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef decision fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef engine   fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef secondary fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef store    fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef data     fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef lib      fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef highlight fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef obs      fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef neutral  fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef overflow fill:#2A0F2A,stroke:#E879F9,stroke-width:2px,color:#F5D0FE
    classDef out      fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef success  fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef pick     fill:#06220F,stroke:#4ADE80,stroke-width:3px,color:#BBF7D0
    classDef dead     fill:#2B0F18,stroke:#F87171,stroke-width:2px,color:#FECACA
    classDef note     fill:#0F1218,stroke:#475569,stroke-width:1.5px,color:#9FB0C9
    linkStyle default stroke:#7C8CA6,stroke-width:1.7px
```

Track A's requests are one-shot: what a request costs at arrival is what it costs
at the end. Track B's are not — **a policy that judges cost at arrival will admit
sessions it cannot finish.** That is the whole reason it is the harder shape.

### Shared vs unique tokens

The brief asks this directly in Part 8. Answering here because it determines
everything about prefix caching and placement.

| Per request | Tokens | Shared or unique |
|---|---|---|
| System prompt + 4 tool schemas | ~1,000 | **Shared** — byte-identical every turn, every session, every user |
| The spoken question, transcribed | ~40 | Unique per session |
| Fetched source text | ~12,000 | Unique, arrives in one turn |
| Conversation so far | grows each turn | Unique per session |

All **predicted**; measured at [Part 8](#part-8--proof-measure-the-app-not-a-generic-trace).

Tools are AgentShip builtins — `web_search`, `scrape_url`, `http_request`,
`calculator`. Nothing new is written on the app side.

→ [`docs/project/00-use-case.md`](docs/project/00-use-case.md)

## Part 1 — Capacity on paper

The brief's formula, on the GPU we actually ran:

```
max_concurrent_seqs ≈ (HBM − weights − activations) / (kv_bytes_per_token × max_len)
```

### `kv_bytes_per_token`

`Qwen2.5-7B-Instruct`: 28 layers, 28 Q heads, **4 KV heads**, head_dim 128.

```
kv_bytes_per_token = 2 (K,V) x 28 layers x 4 kv_heads x 128 head_dim x 2 B
                   = 57,344 B  =  56 KiB per token
```

**We did switch models, and here is what it did to bytes/token** — the brief asks
for exactly this:

| Considered | layers × kv_heads | KV/token | vs chosen |
|---|---|---|---|
| **`Qwen2.5-7B`** (chosen) | 28 × 4 | **56 KiB** | — |
| `Qwen2.5-3B` | 36 × 2 | 36 KiB | 0.6× — cheaper, but a weaker researcher |
| `Qwen3-4B` | 36 × 8 | 144 KiB | **2.6× more**, for 1.9× *fewer* params |
| `Qwen2.5-14B` | 48 × 8 | 192 KiB | 3.4× more |
| `Qwen2.5-32B` | 64 × 8 | 256 KiB | 4.6× more |

**Parameter count does not predict cache cost.** Qwen3-4B is 47% smaller than
our 7B and costs 2.6× more per token, because it has twice the KV heads and 8
more layers. Quantization changes the *weights*; only architecture changes
bytes/token.

### The budget, per slice

`gpu_memory_utilization = 0.90` applies to the **slice**, not the card — HAMi
gives the pod a hard `gpumem` cap that it sees as its entire device. Reading the
0.90 against the card inflates KV by 3×.

```
any worker:  12 GiB x 0.90  =  10.80 GiB budget
             - overhead      =   6.53 GiB   <- MEASURED per slice, stage 2
                                ----------      (predicted 6.00; 7.17 on the
             KV              =   4.27 GiB        whole card at stage 0)

one 16k sequence = 56 KiB x 16,384 = 0.875 GiB    <- P6, measured, held
concurrency      = 4.27 / 0.875   = 4.88 seq/replica
decode pool (x2)                  = 9.8 concurrent
```

### At `max_len` and at the length the app actually sends

The brief asks for both. They differ by 16×, which is the point.

| Length | Why this length | GiB/seq | % of a cache | seq/replica | **decode pool** |
|---|---|---|---|---|---|
| 16,384 (`max_len`) | engine ceiling | 0.875 | 20% | 4.88 | **9.8** |
| ~15,400 (turn 4, worst real request) | 1k prefix + 12k source + ~2.4k history | 0.822 | 19% | 5.2 | 10.4 |
| ~8,100 (mean across a session) | turns 1–4 averaged | 0.433 | 10% | 9.9 | 19.7 |
| ~1,040 (turn 1) | prefix + question only | 0.056 | 1% | 76.3 | 152.5 |

The fleet serves **9.8 concurrent sessions in the worst case and ~20 at the
average request size** (measured; predicted 11.0 and 22.2). Admission matters in the first regime and not the second
— which is why load level is the x-axis of the experiment in Part 8.

### The decision chain

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart TD
    G["1 · GPU<br/>1 × A100-40GB · one node"]
    GX["H100-80 — not available<br/>4 cards — not available, and"]

    M["2 · MODEL<br/>Qwen2.5-7B-Instruct"]
    MX["32B int4 — 18.1 GiB won't fit a 12 GiB slice<br/>14B int4 — 1.0 GiB KV left → 0.3 seq, refuses"]

    F{"3 · FITS bf16?<br/>14.19 GiB weights"}
    FX["12 GiB slice → 10.80 budget → will not load<br/>two 18 GiB slices → 0.81 GiB KV → 0.93 seq"]

    Q["4 · QUANTIZE — AWQ int4<br/>4.27 GiB KV measured"]
    QX["forced, not chosen. fp8 needs sm89+<br/>and the A100 has no fp8 tensor cores,"]

    P["5 · PARALLELISM — TP=1<br/>a worker fits one slice"]
    PX["TP>1 — nothing to shard, and HAMi<br/>slices of one card are not"]

    R{"6 · HOW MANY WORKERS?<br/>brief floor: at least two"}
    TOPO["→ Part 2<br/>1 prefill + 2 decode"]

    FIN["FINAL · 1×A100-40 · Qwen2.5-7B-AWQ int4 · TP=1<br/>3 HAMi slices × 12 GiB · 1 prefill + 2 decode"]

    G --> M --> F
    F -->|"NO"| FX
    FX --> Q --> P --> R --> TOPO ==> FIN

    G -.-|"rejected"| GX
    M -.-|"rejected"| MX
    Q -.-|"why"| QX
    P -.- PX

    class G,M,Q,P,TOPO,FIN pick
    class F,R gate
    class FX dead
    class GX,MX,QX,PX note

    classDef actor    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef input    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef ext      fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef accent   fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef ours     fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef primary  fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef alt      fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef gate     fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef decision fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef engine   fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef secondary fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef store    fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef data     fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef lib      fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef highlight fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef obs      fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef neutral  fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef overflow fill:#2A0F2A,stroke:#E879F9,stroke-width:2px,color:#F5D0FE
    classDef out      fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef success  fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef pick     fill:#06220F,stroke:#4ADE80,stroke-width:3px,color:#BBF7D0
    classDef dead     fill:#2B0F18,stroke:#F87171,stroke-width:2px,color:#FECACA
    classDef note     fill:#0F1218,stroke:#475569,stroke-width:1.5px,color:#9FB0C9
    linkStyle default stroke:#7C8CA6,stroke-width:1.7px
```

Two boxes deserve reading twice.

**Step 3 is the trap.** 7B at bf16 is 14.19 GiB against a 10.80 GiB budget — it
will not load at all on a 12 GiB slice. Widen to two 18 GiB slices and it *does*
load, leaving 0.81 GiB for KV where one 16k sequence needs 0.875 — so the pod
reports `READY 1/1` and then refuses every request that arrives. A config check
that only compares weights against available memory lets this straight through;
it has to require room for **at least one sequence at `max_model_len`**.

**Step 4 is forced, not chosen**, and it is worth being precise about why. On an
sm80 card there are no fp8 tensor cores, so the usual "quantize the weights to
fp8, keep KV in fp16" move does not exist. int4 via AWQ is the only thing that
makes a 7B fit three ways on a 40 GiB card. On a larger card we would have
*declined* to quantize — freeing memory would have tripled concurrency and left
nothing for admission to bind on. Here it is the price of entry.

### Which limiter do we expect first?

**KV.** Written down before the run.

| Candidate | Why not |
|---|---|
| weights | 5.20 GiB of a 7.17 GiB measured overhead, in a 10.80 GiB budget |
| **KV** | **4.27 GiB / 0.875 GiB = 4.88 sequences. This binds** |
| compute | 5 concurrent sequences is a small batch |
| interconnect | TP=1, so no all-reduce. The hop stays inside one card |
| scheduler | `max_num_seqs` 64 is ~13× what KV allows |

Why KV binds is worth stating plainly: **48% of each slice is model weights.**
Replicating three times costs 15.6 GiB of the 36 GiB allocated, leaving 14.4 GiB
of KV across the fleet where a single whole-card worker would have had 26.6 GiB.
**Replication costs 12.2 GiB of KV** — that is the price of having three
addressable workers, and it is the central trade of the design.

Checked at [Part 8](#part-8--proof-measure-the-app-not-a-generic-trace): if
`kv_cache_usage_perc` saturates before `num_requests_waiting` grows, the
hypothesis held.

→ [`docs/project/01-capacity.md`](docs/project/01-capacity.md)

## Part 2 — Design the cluster

| # | Item | Choice | Why |
|---|---|---|---|
| 1 | **GPU** | 1 × A100-40GB, one node | What is obtainable. sm80, so no fp8 — which forces int4 and shapes everything downstream |
| 2 | **model** | `Qwen2.5-7B-Instruct-AWQ` int4 | 56 KiB/token against a 16k app length. See the chain above |
| 3 | **topology** | prefill/decode split, 1 + 2, on **HAMi** slices | Sliced because one card must yield three addressable workers. Split so the hop is never a no-op |
| 4 | **concurrency** | `max_num_seqs` 64, `max_model_len` 16,384 | 64 sits far above what KV allows, so the cache runs out before the scheduler does — if it were lower, a KV policy could never fire |
| 5 | **hop backend** | Mooncake | The only one of the three that moves bytes. `nccl.py` and `nixl.py` return immediately |
| 6 | **overflow** | **`Qwen/Qwen3.5-4B` on Superlinked** (`OVERFLOW_BACKEND=superlinked`) | On 503/529 only, never 429. Two limiters, both named: `OVERFLOW_MAX_TOKENS=64` caps the answer, `OVERFLOW_MAX_REQS=20` caps the **lifetime** of the gateway process — not a rate. **A 4B answering for a 7B is a relief valve, not equivalent service**, so overflowed responses are never pooled with local ones when reporting anything about quality |

### Gateway and engine are two different boxes

| | Owns |
|---|---|
| **`control/`** (ours) | guard, admit, place, our queue, hop, declare warm, stay-vs-leave |
| **Engine** (vLLM) | waiting queue, block table, preemption, kernels, `fcfs`, chunked prefill |

We set engine flags. We do not reimplement its scheduler.

### Topology

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart TD
    S{"Topology on one 40 GiB card<br/>≥2 workers · want hop AND placement"}

    A["A · 3 colocated replicas<br/>3 × 12 GiB · pool 14.6"]
    B["B · 1 prefill + 2 decode<br/>3 × 12 GiB · pool 9.8"]
    C["C · 2 prefill + 2 decode<br/>4 × 9 GiB · pool ~2"]
    D["D · 1 prefill + 1 decode<br/>2 × 18 GiB · pool ~11"]

    AX["src == dst always → no hop<br/>Part 6 becomes warmup-only"]
    CX["9 GiB slice → only 2.1 GiB KV<br/>→ 2.4 seq/replica · pool 4.8"]
    DX["one decode worker → pick() has no choice<br/>that is routing, not placement"]

    FIN["CHOSEN — B<br/>hop always crosses workers"]

    S --> A & B & C & D
    A -.-|"rejected"| AX
    C -.-|"rejected"| CX
    D -.-|"rejected"| DX
    B ==> FIN

    class B,FIN pick
    class S gate
    class A,C,D dead
    class AX,CX,DX note

    classDef actor    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef input    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef ext      fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef accent   fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef ours     fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef primary  fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef alt      fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef gate     fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef decision fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef engine   fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef secondary fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef store    fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef data     fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef lib      fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef highlight fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef obs      fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef neutral  fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef overflow fill:#2A0F2A,stroke:#E879F9,stroke-width:2px,color:#F5D0FE
    classDef out      fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef success  fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef pick     fill:#06220F,stroke:#4ADE80,stroke-width:3px,color:#BBF7D0
    classDef dead     fill:#2B0F18,stroke:#F87171,stroke-width:2px,color:#FECACA
    classDef note     fill:#0F1218,stroke:#475569,stroke-width:1.5px,color:#9FB0C9
    linkStyle default stroke:#7C8CA6,stroke-width:1.7px
```

**One prefill worker is enough** because prefill builds KV and frees it
immediately — it never holds a live sequence, so it is not what gates
concurrency. Two decode workers is the minimum for `pick` to be a real decision.

### What slicing costs, and what `gpucores` actually is

The concept most easily got wrong: **HAMi's `gpucores` caps a pod's share of SM
*time*; it does not give it dedicated SMs.** So the usual argument for
disaggregation — that a long prefill cannot stall another worker's decode loop —
only partly holds, because all three pods contend for the same SMs through a
scheduler quota. On three separate cards the isolation would be physical.

**Any inter-token latency spike on decode during a large prefill is this
effect**, and it would not appear on a multi-card fleet. We expect to see it and
we will say so — it is prediction P7.

### If we scale, which pool?

- **uncached prefill tokens rising** → prefill is the constraint → add prefill
- **decode slots hot / `num_requests_waiting` growing on decode** → add decode

"Add a replica" is not an answer. On this card a replica costs 5.2 GiB of
duplicated weights straight out of fleet KV, so it multiplies KV pressure rather
than relieving it.

### The integrity check, before any run is believed

```
kubectl get pods -o wide                                   # 3 vllm pods, Running
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
#  -> 3 processes on index 0, each ~10-11 GiB, none above its slice
```

The failure mode is the inverse of a multi-card fleet's. There, the danger is two
pods landing on one card. Here it is **one pod exceeding its slice and quietly
taking memory from another** — so the check is that HAMi is enforcing the cap.

Pods start **one at a time**; three contending at startup can leave one with half
the KV of its peers, and a fleet with unequal caches makes the pool-wide
concurrency figure wrong.

→ [`docs/project/02-cluster.md`](docs/project/02-cluster.md) ·
[`docs/cluster/infrastructure.md`](docs/cluster/infrastructure.md)

## Part 3 — Guardrails, admit, stay vs leave

```
inspect(payload) → Guard
should_shed(req, snap) → (shed?, code, reason, retry_after_seconds)
```

Three gates, cheapest first. A refusal never costs a GPU.

| Gate | Question | Code | Leaves? |
|---|---|---|---|
| `inspect` | well-formed? model in allowlist? token limit sane? | 400 | no |
| tenant window | is this caller over their share? | 429 | **never** |
| `should_shed` | can the fleet serve this now, in time? | 503 | yes, up to the cap |
| — | upstream overloaded | 529 | yes, same cap |

The tenant window runs **before** the capacity question, deliberately: a 429 is
client-attributable, and **a 429 that overflowed is on the brief's bad-answer
list** — paying a provider for a request we are refusing on policy grounds is
overspending, not a capacity event.

### Which signal should admission watch?

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart TD
    S{"What does the policy read?"}
    KV["kv_pressure<br/>vllm:kv_cache_usage_perc"]
    QD["queue_depth<br/>vllm:num_requests_waiting"]
    DL["deadline slack<br/>will it miss TTFA / full answer?"]
    TQ["tenant quota<br/>token window per caller"]

    KVY["KV is the limiter (Part 1)<br/>so this is the signal that can"]
    QDX["used as a placement scorer instead<br/>(Part 4) — the brief requires it"]
    DLX["tempting, because voice gives a real<br/>deadline — but it needs a finish-time"]
    TQX["kept, as the tenant window above —<br/>a different question from capacity"]

    FIN["CHOSEN — kv_threshold @ 0.90<br/>Class A: pure function of request + state"]

    S --> KV & QD & DL & TQ
    KV --> KVY ==> FIN
    QD -.-> QDX
    DL -.-> DLX
    TQ -.-> TQX

    class KV,KVY,FIN pick
    class S gate
    class QD,TQ,DL alt
    class QDX,DLX,TQX note

    classDef actor    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef input    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef ext      fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef accent   fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef ours     fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef primary  fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef alt      fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef gate     fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef decision fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef engine   fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef secondary fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef store    fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef data     fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef lib      fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef highlight fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef obs      fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef neutral  fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef overflow fill:#2A0F2A,stroke:#E879F9,stroke-width:2px,color:#F5D0FE
    classDef out      fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef success  fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef pick     fill:#06220F,stroke:#4ADE80,stroke-width:3px,color:#BBF7D0
    classDef dead     fill:#2B0F18,stroke:#F87171,stroke-width:2px,color:#FECACA
    classDef note     fill:#0F1218,stroke:#475569,stroke-width:1.5px,color:#9FB0C9
    linkStyle default stroke:#7C8CA6,stroke-width:1.7px
```

Admission is a **socket, not a rule** — `ADMISSION_POLICY` selects from
AdmitPerf's registry with no code change. **The baseline is `no_admission`, not
the gate switched off**: with the gate off nothing is logged, so the comparison
would have one side.

Three details that each prevent a report that looks fine and is wrong:

- **A missing metric is omitted, never zeroed.** `kv_cache_usage: 0.0` reads as a
  completely empty cache — the one value that guarantees every policy admits. A
  failed scrape would silently switch admission off.
- **Pressure comes from the replica with the most headroom**, not the average,
  because that is where the router will place. An average lets one idle replica
  hide a saturated fleet.
- **The policy fails open**, with `admitperf_gate_active` as a separate metric, so
  a crashing policy is distinguishable from `no_admission`.

→ [`docs/project/03-admission.md`](docs/project/03-admission.md) ·
[`docs/cluster/gateway.md`](docs/cluster/gateway.md)

## Part 4 — Place

> **Prefix-aware prefill placement is inert against real vLLM.**
> `VLLMWorker.snapshot()` leaves `prefix_tokens=None`, so `score_prefix_cache`
> returns `None` and contributes nothing — prefill falls back to load and KV
> headroom. It works in simulation because `FakeWorker` populates the field,
> which is why the tests pass over a dead path. See
> [`docs/project/real-vs-simulated.md`](docs/project/real-vs-simulated.md).

```
pick(req, workers, *, policy) → Worker | Shed
```

Two decode candidates, so this is a real decision. **Prefill and decode use
different scorers**, and **queue depth is a scorer here, not only an admit
input**, as the brief requires.

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart TD
    S{"Which policy?"}
    LL["least_loaded<br/>active + queue depth + KV headroom"]
    P2C["p2c<br/>power of two choices"]
    PTL["prefix_then_load<br/>cache locality first, then load"]

    P2CX["needs ≥3 candidates to beat<br/>least_loaded. We have 2 decode"]

    PFP["prefill → prefix_then_load<br/>a worker holding the 1k shared"]
    DCP["decode → least_loaded<br/>prefix is irrelevant: the KV"]

    FIN["CHOSEN — a different scorer per pool<br/>never bounce: a shed is a shed,"]

    S --> LL & P2C & PTL
    P2C -.-|"rejected"| P2CX
    PTL ==> PFP
    LL ==> DCP
    PFP & DCP ==> FIN

    class LL,PTL,PFP,DCP,FIN pick
    class S gate
    class P2C dead
    class P2CX note

    classDef actor    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef input    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef ext      fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef accent   fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef ours     fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef primary  fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef alt      fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef gate     fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef decision fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef engine   fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef secondary fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef store    fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef data     fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef lib      fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef highlight fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef obs      fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef neutral  fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef overflow fill:#2A0F2A,stroke:#E879F9,stroke-width:2px,color:#F5D0FE
    classDef out      fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef success  fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef pick     fill:#06220F,stroke:#4ADE80,stroke-width:3px,color:#BBF7D0
    classDef dead     fill:#2B0F18,stroke:#F87171,stroke-width:2px,color:#FECACA
    classDef note     fill:#0F1218,stroke:#475569,stroke-width:1.5px,color:#9FB0C9
    linkStyle default stroke:#7C8CA6,stroke-width:1.7px
```

| Pool | Scorers, in order |
|---|---|
| prefill | prefix-cache locality → uncached prefill tokens → KV headroom |
| decode | active requests → **queue depth** → KV headroom |

**Do not bounce.** If `pick` returns `Shed`, the request is refused with a reason
— it is not re-offered to another worker, which would turn one refusal into three
attempts' worth of latency and make the shed-by-reason panel meaningless.

**One limitation, named rather than implied.** The decode destination is chosen
*before* prefill runs. A 12k prefill takes on the order of a second, and by then
the decode worker picked may no longer be the least loaded. Deciding at transfer
time would place better. Named so that any decode imbalance in the run is
attributed here and not to the admission policy.

→ [`docs/project/04-placement.md`](docs/project/04-placement.md) ·
[`docs/cluster/router.md`](docs/cluster/router.md)

## Part 5 — Queue

```
admit → place → our queue → engine waiting / running / preempt
```

**The notebook is the deliverable for this part** —
[`notebook/part5-queue.ipynb`](notebook/) — because every answer below needs a
live scrape, not an assertion. *Status: not written.*

| Question | Answer | Source |
|---|---|---|
| Who sits in **our** queue vs vLLM's **waiting** queue? | Our control plane holds **no queue** — requests block on a thread. The real queue is inside the engine. That is why `queue_depth` is scraped from `/metrics` rather than read from a local structure, and why `defer` means "come back", not "you are 12th" | scrape |
| waiting / running / swapped? | *TBM* | `vllm:num_requests_{waiting,running,swapped}` |
| `orch_replica_queue_depth` — which pod, under which mix? | *TBM* | per-pod scrape |
| A 16k research retrieve and a short agent step are both ready — who goes first, us or the engine? | **Us, then the engine.** We admit and place; the engine orders within its own batch by `fcfs` + chunked prefill. We do not reorder inside it | notebook |
| PagedAttention **packs**, prefix cache **reuses** — which saved memory on our mix? | **Prefix cache**, predicted: ~1k of system prompt + tool schemas is identical across every turn and session. Paging prevents fragmentation but does not dedupe | `prefix_cache_hit_rate` |
| Chunked prefill / continuous batching flags, and why? | `--enable-prefix-caching` for the shared prefix; `--max-num-seqs 64` deliberately above the KV limit so the cache binds first; `--max-num-batched-tokens` *TBM* — a 12k prefill must not starve decode, which matters more here because voice makes the stall audible | config + scrape |
| KV full after admit — shed at the door, or engine preempts? | **Shed at the door.** `kv_threshold` 0.90 leaves headroom precisely so the engine does not have to preempt a sequence it already started. A non-zero preemption count means the gate let through more than the fleet could hold | `vllm:num_preemptions` |
| Client gone (`aborted`) — who frees the KV, and how? | The engine. We mark `Request.aborted` and stop reading; vLLM drops the sequence and returns its blocks. **Voice makes this common** — a listener who talks over the answer aborts mid-generation | notebook |
| After a worker returns — slam it at 100%, or ramp? | **Ramp.** `READY` precedes serving by minutes while CUDA graphs capture. Slamming a just-returned worker quotes a TTFT that is warmup, not inference | [Part 6](#part-6--hop-when-do-you-move-kv-is-the-replica-warm) |

→ [`docs/project/05-queue.md`](docs/project/05-queue.md)

## Part 6 — Hop: when do you move KV? Is the replica warm?

> **Under review.** Reading the code showed `kvbus.transfer()` records a hop and
> re-registers the prefix; it does not move KV blocks — vLLM owns those. The
> economics below compare against a transfer that does not currently happen, and
> the claim that Mooncake "moves bytes" is wrong: it is a 78-line hop recorder,
> identical in modules 9b, 10 and here. See
> [`docs/project/topology.md`](docs/project/topology.md) for what a real split
> requires and the three options. This section is rewritten once that is decided.

With topology B, **`src != dst` always** — prefill is pod 0, decode is pod 1 or 2
— so the hop is never a no-op and there is always a transfer to record.

| Case | What we do |
|---|---|
| `src == dst` | Cannot happen in this topology. Would be: do nothing |
| `src != dst` | Record a hop: from `prefill-0`, to `decode-{0,1}`, this many tokens, this prefix, backend `mooncake` |

### Is the move worth it?

```
a 12k prompt produces 12,288 x 56 KiB = 0.656 GiB of KV

recompute on the decode worker   ~2.5-3.7 s   (predicted — 2·N·T FLOPs at 28% of an A100-40)
move via Mooncake, same card     ~0.5-0.8 s   (prior, not prediction — see below)
```

The ~0.5–0.8 s figure is **borrowed from a published single-GPU run**, not
derived: a comparable stack measured LMCache hop-plus-store-barrier at 0.5–0.8 s
and NIXL pod-to-pod over TCP at ~4 s. Our own first-principles estimate for a
device→host→device copy of 0.656 GiB was ~150 ms, which that measurement says is
optimistic by 3–5×. **We quote the measured prior and will replace it with our
own number.** If the transport lands near 4 s, the hop is slower than recomputing
and the conclusion flips.

The honest comparison is not against recompute, though — it is against **not
splitting at all**: a colocated replica (topology A) keeps the KV in place and
moves nothing. Disaggregation's cost on one card is this transfer; what it buys
is phase separation, and `gpucores` time-slicing only partly delivers that.

### Which backend

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart TD
    S{"Hop backend"}
    MC["mooncake<br/>store + transfer engine"]
    NC["nccl.py"]
    NX["nixl.py"]

    NCX["four-line stub — returns immediately<br/>moves no bytes, records nothing."]

    FIN["CHOSEN — mooncake<br/>and KV_BACKEND is checked before"]

    S --> MC & NC & NX
    NC -.-|"rejected"| NCX
    NX -.-|"rejected"| NCX
    MC ==> FIN

    class MC,FIN pick
    class S gate
    class NC,NX dead
    class NCX note

    classDef actor    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef input    fill:#2B0F18,stroke:#FB7185,stroke-width:2px,color:#FECDD3
    classDef ext      fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef accent   fill:#1E1033,stroke:#C084FC,stroke-width:2px,color:#E9D5FF
    classDef ours     fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef primary  fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef alt      fill:#0C1A33,stroke:#60A5FA,stroke-width:2px,color:#DBEAFE
    classDef gate     fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef decision fill:#2B2008,stroke:#FBBF24,stroke-width:3px,color:#FDE68A
    classDef engine   fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef secondary fill:#06220F,stroke:#4ADE80,stroke-width:2px,color:#BBF7D0
    classDef store    fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef data     fill:#2A1405,stroke:#FB923C,stroke-width:2px,color:#FED7AA
    classDef lib      fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef highlight fill:#13163A,stroke:#818CF8,stroke-width:2px,color:#C7D2FE
    classDef obs      fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef neutral  fill:#0F172A,stroke:#94A3B8,stroke-width:2px,color:#CBD5E1
    classDef overflow fill:#2A0F2A,stroke:#E879F9,stroke-width:2px,color:#F5D0FE
    classDef out      fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef success  fill:#04302B,stroke:#22D3EE,stroke-width:2.5px,color:#A5F3FC
    classDef pick     fill:#06220F,stroke:#4ADE80,stroke-width:3px,color:#BBF7D0
    classDef dead     fill:#2B0F18,stroke:#F87171,stroke-width:2px,color:#FECACA
    classDef note     fill:#0F1218,stroke:#475569,stroke-width:1.5px,color:#9FB0C9
    linkStyle default stroke:#7C8CA6,stroke-width:1.7px
```

### Is it actually warm?

Two different things get called warmth and only one is about the hop.

| | What it is | The measurement hazard |
|---|---|---|
| **Worker warmth** | `torch.compile` has captured its CUDA-graph buckets | `READY 1/1` appears **minutes** before the engine can serve. A TTFT off a cold replica is the brief's "quoting TTFT from a cold replica as the SLO" |
| **Cache warmth** | the shared prefix is resident in the worker's prefix cache or in Mooncake | After a hop onto a fresh decode replica, was the first token slow because warmup was still finishing? |

**The protocol:** warm every pod with a fixed request, then **re-quote the TTFT**.
The first-agent-step TTFT after a hop onto a newly-started decode replica is
measured both cold and warm, and only the warm number is quoted as an SLO.

Warmth matters more here than in a text app: **a cold replica's first token is
audible as dead air.** The ~1.8 s first-request penalty of single-bucket CUDA
graph capture is invisible in a chat box and glaring in a voice turn.

→ [`docs/project/06-hop.md`](docs/project/06-hop.md) ·
[`docs/cluster/kvbus.md`](docs/cluster/kvbus.md)

## Part 7 — Wire the app to the cluster

Every completion goes through our serve path. **Completions that skip
guardrails / admit / place / queue do not count**, so there is no direct route
from the agent to a worker.

```
spoken turn → Deepgram STT → agent step
        ↓
guardrails.inspect
        ↓
admit  (tenant window, then kv_threshold)
        ↓
place  (pick — prefix_then_load | least_loaded)
        ↓
queue  →  transfer(prefill-0, decode-{0,1})
        ↓
engine  /v1/chat/completions
        ↓
overflow gate   429/500/slice_oom stay · 503/529 may leave
        ↓
Deepgram TTS → spoken answer
```

```yaml
# app/dodona.yaml
model: openai/Qwen2.5-7B-Instruct-AWQ
params:
  api_base: "http://127.0.0.1:8080/v1"
```

**Open WebUI first.** Stage 1 points Open WebUI straight at the engine for hand
testing, before any control plane exists. That way a broken agent and a broken
gateway are never confused for each other.

Three settings that are choices, not defaults:

| Setting | Value | Why |
|---|---|---|
| `temperature` / `top_p` | 0.3 / 0.8 | Qwen2.5's recommended sampling. Greedy decoding degenerates and emits tool calls as prose |
| `scrape_url` truncation | 12k tokens | So a turn-4 request fits in 16,384. An engine context refusal looks nothing like an admission refusal in the decision log |
| tool calls per reply | one | The `hermes` parser returns the *entire batch* as text when any single call is malformed |

Engine side needs `--enable-auto-tool-choice --tool-call-parser hermes`.

**Smoke the engine before debugging the serve path**, in this order:

| # | Check | Passes when |
|---|---|---|
| 1 | `curl` a worker directly | 200, text back — the engine is alive |
| 2 | Open WebUI → engine | you can talk to it in a browser |
| 3 | `curl` the gateway, plain completion | 200 — the serve path works |
| 4 | `curl` the gateway **with `tools`** | response has `tool_calls`, not prose |
| 5 | agent, one spoken question | `web_search` fires |
| 6 | agent, full question | multi-turn, cites a URL it actually fetched |
| 7 | decision log non-empty, `admitperf_gate_recording 1` | one row per completion |

**TTFT comes from vLLM's metrics, never from the client** — and with voice there
are two client-side numbers (first audio, full answer) that are both *downstream*
of engine TTFT, so conflating them would be doubly wrong.

→ [`docs/project/07-wiring.md`](docs/project/07-wiring.md) · [`app/README.md`](app/)

## Part 8 — Proof: measure the app, not a generic trace

Traffic is produced by the agent itself. The load generator exists only to replay
the **same** trace across runs so two runs are comparable.

| Run | Policy | Concurrent sessions | Why |
|---|---|---|---|
| `low-baseline` | `no_admission` | 2 | the other side of the comparison |
| `low` | `kv_threshold` 0.90 | 2 | ~20% of the 9.8 pool; pressure should stay under 0.90 |
| `high-baseline` | `no_admission` | 14 | the other side |
| `high` | `kv_threshold` 0.90 | 14 | past the pool; pressure should reach 0.90 |

Three seeds each. All results *TBM*.

### The eight dashboards

| # | Dashboard | Points at |
|---|---|---|
| 1 | Cluster | *TBM* |
| 2 | Success and failures | *TBM* |
| 3 | Gateway + admission — sheds by reason | `tenant_tokens`, `timeout_queue`, `kv_free` |
| 4 | Router | placement by pod, prefix-match rate |
| 5 | Queue depth by pod | `orch_replica_queue_depth` |
| 6 | vLLM | KV usage, waiting/running, preemptions, prefix hits |
| 7 | Mooncake KV | `/hops`, bytes moved, failed writes, evictions |
| 8 | Pods / replicas | 3 pods on 1 GPU, slice enforcement |

### The brief's questions

| Question | Answer |
|---|---|
| What is the app, which tokens shared vs unique? | [Part 0](#shared-vs-unique-tokens) — ~1k shared prefix, ~12k unique source |
| What dies at guardrails vs admit vs place vs queue? | *TBM* — the shed-by-reason panel is the answer |
| Where do I prevent work that will time out? | `timeout_queue` shed at admit. **Voice makes this concrete**: a request that cannot make its TTFA budget is better refused aloud than admitted and left silent |
| Where do I protect KV? | `kv_threshold` 0.90 at admit, plus KV headroom as a placement scorer |
| Where do I prioritise interactive traffic? | *TBM* (`p99_spread`) |
| Where do I stop one tenant owning the GPU? | the tenant window, before capacity — a 429 that never leaves |
| Where do I hop, and what is **not** copied? | prefill-0 → decode-{0,1} over Mooncake. **Not copied:** the final token's KV (recomputed), anything past the 256-token chunk boundary, and any block already in the destination's prefix cache |
| Where do I evict, and what becomes a ghost if I skip it? | `control/kv_eviction.py`. Skip it and a hopped prefix stays pinned in Mooncake after its session ended — a ghost holding store capacity nothing will read |
| Where does the engine scheduler sit vs our admit/place/queue? | [Part 2](#gateway-and-engine-are-two-different-boxes) |
| What limited concurrency on this GPU for this app? | **KV.** 4.27 GiB per decode worker ÷ 0.875 GiB = **4.88 sequences**, pool 9.8 — measured at stage 2 from the pods themselves; 6.53 GiB of the 10.80 GiB slice budget goes to weights, activations and CUDA graphs |
| Four production alerts? | (1) `admitperf_gate_recording == 0` — deciding but not logging; (2) `vllm:num_preemptions > 0` — the gate admitted more than the fleet holds; (3) hop count flat while prefill tokens rise — the transport silently stopped; (4) p99 time-to-first-audio > 1.5 s — the only SLO the listener can hear |
| If I scale, which pool? | [Part 2](#if-we-scale-which-pool) — prefill tokens → prefill, decode slots → decode |
| At 10× traffic, and three wrong knobs? | **Right:** a bigger card so int4 is no longer forced, then a second prefill pod, then shard sessions by tenant. **Wrong:** raising `max_num_seqs` (the scheduler is not the limit), adding an identical replica (costs 5.2 GiB of weights out of fleet KV), raising the admission threshold (defers the wall, does not move it) |

### Predictions, written before the run

| # | Prediction | Measured | Held? |
|---|---|---|---|
| P1 | KV binds, not the scheduler | *TBM* | *TBM* |
| P2 | Signal reaches 0.90 at 14 sessions (vs a pool of 9.8) | *TBM* | *TBM* |
| P3 | Signal does not reach 0.90 at 2 sessions | *TBM* | *TBM* |
| P4 | Prefix-cache hit rate > 70% | *TBM* | *TBM* |
| P5 | Prompt:completion ratio > 10:1 | *TBM* | *TBM* |
| P6 | `kv_bytes_per_token` = 56 KiB ±5% | **56.0 KiB** | ✅ **held** |
| P7 | Inter-token p95 on decode spikes during a large prefill (`gpucores` is time-sliced) | *TBM* | *TBM* |
| P8 | Mooncake hop ≫ our 150 ms estimate, closer to the 0.5–0.8 s published prior | *TBM* | *TBM* |

P6 comes first in time — read off `config.json` on a live worker. Every
concurrency figure in Part 1 moves with it.

→ [`docs/project/08-proof.md`](docs/project/08-proof.md) ·
[`docs/project/results-report.md`](docs/project/results-report.md)

---

## Bad answers we are explicitly avoiding

| Bad answer | Where we avoid it |
|---|---|
| "`bench latency` batch=8 is the production SLO" | SLOs come from agent traffic, [Part 8](#part-8--proof-measure-the-app-not-a-generic-trace) |
| "Cache is full, add another replica" | [Part 2](#if-we-scale-which-pool) — a replica costs 5.2 GiB of fleet KV |
| "NCCL/NIXL moves KV tensors" | [Part 6](#which-backend) — both are stubs |
| "A replica is ready when weights are on the GPU" | [Part 6](#is-it-actually-warm) — warm, then re-quote |
| "Overflow is Superlinked / another API" with no model and no limiter named | [Part 2](#part-2--design-the-cluster) item 6 — the brief names this one directly. Ours: `Qwen/Qwen3.5-4B`, capped by `OVERFLOW_MAX_TOKENS=64` and `OVERFLOW_MAX_REQS=20` lifetime |
| "I reimplemented vLLM's scheduler" | [Part 2](#gateway-and-engine-are-two-different-boxes) |
| A 429 that overflowed | [Part 3](#part-3--guardrails-admit-stay-vs-leave) — ordering is load-bearing |
| "RAG is a third phase" | Two phases: prefill, decode |
| Wall seconds across models without token counts | Every comparison carries tokens |
| TTFT from a cold replica | [Part 6](#is-it-actually-warm) |

## What is built and what is not

Dodona is built **stage by stage** — nothing is inherited wholesale. Runbook:
[`RUNBOOK.md`](RUNBOOK.md).

| | |
|---|---|
| The design, the arithmetic, the decision trees | **This document** |
| Stages 0–10 | **None started.** See [`README.md`](README.md#status) |
| Any measured number | **None.** All `TBM` |

Reference implementations we port from, piece by piece, rather than run as a
black box: the course lab (gateway, router, Mooncake, HAMi, dashboards) and
AdmitPerf (policy registry, decision log, report).
