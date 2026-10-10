<p align="center">
  <img src="assets/banner.svg" alt="Dodona" width="100%">
</p>

<p align="center">
  <b>Ask aloud. It searches, reads the sources, and answers out loud with citations.</b><br>
  <sub>Every token of inference on one GPU you rent. No hosted chat API in the answer path.</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/GPU-1%C3%97%20A100--40GB-047857?style=flat-square" alt="1x A100-40GB">
  <img src="https://img.shields.io/badge/model-Qwen2.5--7B--Instruct--AWQ%20int4-047857?style=flat-square" alt="Qwen2.5-7B-Instruct-AWQ int4">
  <img src="https://img.shields.io/badge/max__model__len-16%2C384-047857?style=flat-square" alt="max_model_len 16384">
  <img src="https://img.shields.io/badge/decode%20pool-11%20sequences-047857?style=flat-square" alt="decode pool 11 sequences">
  <img src="https://img.shields.io/badge/stage-0%20of%2010-64748b?style=flat-square" alt="stage 0 of 10">
</p>

<p align="center">
  <a href="https://github.com/vllm-project/vllm"><img src="https://img.shields.io/badge/engine-vLLM-10b981?style=flat-square" alt="vLLM"></a>
  <a href="https://github.com/Project-HAMi/HAMi"><img src="https://img.shields.io/badge/GPU%20slicing-HAMi-10b981?style=flat-square" alt="HAMi"></a>
  <a href="https://github.com/kvcache-ai/Mooncake"><img src="https://img.shields.io/badge/KV%20transport-Mooncake-10b981?style=flat-square" alt="Mooncake"></a>
  <a href="https://k3s.io"><img src="https://img.shields.io/badge/k3s-orchestration-10b981?style=flat-square&logo=kubernetes&logoColor=white" alt="k3s"></a>
  <a href="https://prometheus.io"><img src="https://img.shields.io/badge/metrics-Prometheus-10b981?style=flat-square&logo=prometheus&logoColor=white" alt="Prometheus"></a>
  <a href="https://grafana.com"><img src="https://img.shields.io/badge/dashboards-Grafana-10b981?style=flat-square&logo=grafana&logoColor=white" alt="Grafana"></a>
</p>

<p align="center">
  <a href="https://lambdalabs.com"><img src="https://img.shields.io/badge/compute-Lambda-334155?style=flat-square" alt="Lambda Cloud"></a>
  <a href="https://deepgram.com"><img src="https://img.shields.io/badge/voice-Deepgram%20%C2%B7%20off--GPU-334155?style=flat-square" alt="Deepgram, off-GPU"></a>
  <a href="https://superlinked.com"><img src="https://img.shields.io/badge/overflow-Superlinked%20%C2%B7%20Qwen3.5--4B-334155?style=flat-square" alt="Superlinked overflow"></a>
  <a href="https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-AWQ"><img src="https://img.shields.io/badge/weights-Hugging%20Face-334155?style=flat-square&logo=huggingface&logoColor=white" alt="Hugging Face"></a>
</p>

<p align="center">
  <a href="https://github.com/Agent-Ship/agent-ship"><img src="https://img.shields.io/badge/agent-AgentShip-065f46?style=flat-square" alt="AgentShip"></a>
  <a href="https://github.com/harshuljain13/AdmitPerf"><img src="https://img.shields.io/badge/admission-AdmitPerf-065f46?style=flat-square" alt="AdmitPerf"></a>
  <a href="https://github.com/langchain-ai/langgraph"><img src="https://img.shields.io/badge/graph-LangGraph-065f46?style=flat-square" alt="LangGraph"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-065f46?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+">
</p>

<p align="center">
  <a href="#the-problem">Problem</a> &nbsp;·&nbsp;
  <a href="#how-it-works">How it works</a> &nbsp;·&nbsp;
  <a href="#the-numbers">Numbers</a> &nbsp;·&nbsp;
  <a href="#quick-start">Quick start</a> &nbsp;·&nbsp;
  <a href="PROJECT.md">Project</a> &nbsp;·&nbsp;
  <a href="SUBMISSION.md">Design</a>
</p>

---

> *The oracle at Dodona answered questions by reading the sound of wind through
> the oak leaves.* Voice in, sourced answer out.

## The problem

**Self-hosting a voice research agent on one GPU is hard in a specific way, and
voice makes the failure audible.**

| | |
|---|---|
| **The requests are enormous and uneven** | One fetched source is ~12,000 tokens — 0.66 GiB of KV, a seventh of a worker's cache, for one caller. The turn before it was 1,040 tokens. Nothing about turn 1 tells you what turn 4 will cost |
| **There is one card** | An A100-40GB split three ways gives a decode pool of **11 concurrent sequences**. Not 11 users — 11 *sequences*. A handful of listeners saturates it |
| **Someone is waiting out loud** | Latency is dead air, not a percentile. And one request carries two promises: first audio inside a second, full answer in ten to thirty |

So the cluster is deciding what to let in, dozens of times a minute. **Every
deployment makes that decision — most make it by accident**, when a queue fills
and everything slows together. That accident refuses nothing, serves everything
badly, and tells you nothing about why.

**Dodona makes the decision on purpose, and writes down what it saw.**

Because a policy whose signal never reached its threshold is indistinguishable
from one that worked — same latencies, same dashboards. So every admission
decision records the value it read, and every run ends with a verdict on whether
the policy could have fired at all.

→ The problem in full, and the decisions it forces: [**`PROJECT.md`**](PROJECT.md)

## How it works

One turn, three times over — cheap, expensive, and refused. The numbers on each
line are the ones from [the arithmetic](docs/project/01-capacity.md); they are
*predicted* until stage 10 measures them.

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'actorBkg':'#eff6ff','actorBorder':'#2563eb','actorTextColor':'#1e3a8a','actorLineColor':'#cbd5e1',
  'signalColor':'#475569','signalTextColor':'#1e293b',
  'noteBkgColor':'#ffffff','noteBorderColor':'#94a3b8','noteTextColor':'#1e293b',
  'activationBkgColor':'#dcfce7','activationBorderColor':'#16a34a'}}}%%
sequenceDiagram
    autonumber
    actor L as 🎙 Listener
    participant DG as Deepgram<br/>off-GPU
    participant AG as Research<br/>agent
    participant CP as Control<br/>plane
    participant E as vLLM fleet<br/>1 prefill · 2 decode

    rect rgb(240, 253, 244)
    Note over L,E: TURN 1 — cheap. The 1k system prompt is a shared prefix, identical every turn.
    L->>DG: "which herbs interact with warfarin?"
    DG->>AG: text · ~40 tokens
    AG->>CP: completion · ~1,040 tokens
    CP->>CP: kv_pressure 0.18 < 0.90 → ADMIT
    CP->>E: place → prefill-0, hop → decode-1
    E-->>AG: tool_call · web_search
    AG-->>DG: "let me look that up"
    DG-->>L: ✅ first audio in < 1 s
    end

    rect rgb(255, 251, 235)
    Note over L,E: TURN 3 — one fetched source arrives as a single 12k prefill.
    AG->>CP: completion · ~14,000 tokens
    CP->>CP: kv_pressure 0.81 < 0.90 → ADMIT
    CP->>E: 0.66 GiB of KV — 14% of a decode worker
    E-->>AG: answer, with citations
    DG-->>L: ✅ full answer in 10–30 s
    end

    rect rgb(254, 242, 242)
    Note over L,E: UNDER LOAD — 14 sessions against a pool of 11. The cache is the limit.
    AG->>CP: completion · ~15,400 tokens
    CP->>CP: kv_pressure 0.93 ≥ 0.90 → REFUSE
    CP-->>AG: 503 · reason=kv_free · retry_after=2s
    DG-->>L: 🔇 "I'm at capacity" — spoken, not dead air
    end

    Note over CP: Every one of those three decisions<br/>writes down the value it read.
```

**Read the third block first.** It is the one the project exists for. A cluster
without a control plane does not refuse — it accepts all fourteen, the queue
fills, every listener waits, and nobody is told why. Refusing one caller out loud
is the better failure, and you only get to choose it if something is watching the
cache.

And `0.18 → 0.81 → 0.93` is the other half: **the signal has to actually move.**
If it sat at 0.02 all run, the policy was installed and inert, and no dashboard
would have told you.

<details>
<summary><b>The same thing as a component diagram</b></summary>

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'14px',
  'lineColor':'#64748b','clusterBkg':'#fbfdff','clusterBorder':'#93c5fd',
  'edgeLabelBackground':'#ffffff'}}}%%
flowchart LR
    L(["Listener"])
    DG["Deepgram<br/>STT · TTS"]
    AG[["Research agent<br/>search · fetch · cite"]]

    subgraph CP["Control plane — what we write"]
        direction LR
        GD[/"guard<br/>malformed → 400"/]
        AD{"admit<br/>kv_pressure ≥ 0.90?"}
        PL["place<br/>which decode replica"]
        HP["hop<br/>KV over Mooncake"]
        GD -->|"well-formed"| AD -->|"admitted"| PL --> HP
    end

    AP[["AdmitPerf<br/>policy + decision log"]]
    RF(["503 · kv_free"])
    RP(["run report<br/>SIGNAL LIVENESS"])

    subgraph GPU["1 × A100-40GB — three HAMi slices of 12 GiB"]
        direction LR
        PF["vLLM prefill ×1<br/>builds KV, frees it"]
        MC[("Mooncake<br/>KV store")]
        DC["vLLM decode ×2<br/>4.80 GiB KV · 5.5 seq each"]
        PF -->|"0.66 GiB"| MC --> DC
    end

    L --> DG --> AG --> GD
    HP --> PF
    DC -->|"tokens"| AG --> DG --> L
    AD -->|"refused"| RF
    AD <-->|"decide(req, state)"| AP --> RP
    DC -. "kv_cache_usage_perc" .-> AD

    class L actor
    class DG,AG ext
    class GD,PL,HP ours
    class AD gate
    class PF,DC engine
    class MC store
    class AP highlight
    class RF dead
    class RP out

    classDef actor  fill:#fff1f2,stroke:#e11d48,stroke-width:2px,color:#881337
    classDef ext    fill:#faf5ff,stroke:#9333ea,stroke-width:2px,color:#581c87
    classDef ours   fill:#eff6ff,stroke:#2563eb,stroke-width:2px,color:#1e3a8a
    classDef gate   fill:#fffbeb,stroke:#d97706,stroke-width:2.5px,color:#78350f
    classDef engine fill:#f0fdf4,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef store  fill:#fff7ed,stroke:#ea580c,stroke-width:2px,color:#7c2d12
    classDef highlight fill:#eef2ff,stroke:#4f46e5,stroke-width:2px,color:#312e81
    classDef dead   fill:#fef2f2,stroke:#dc2626,stroke-width:2px,color:#7f1d1d
    classDef out    fill:#f0fdfa,stroke:#0d9488,stroke-width:2px,color:#134e4a
    linkStyle default stroke:#64748b,stroke-width:1.6px
```

🔵 what we write · 🟢 the engine, configured not touched · 🟠 KV storage ·
🟡 the decision · 🟣 off-GPU · 🔴 a refusal

</details>

## The numbers

```
1 × A100-40GB · Qwen2.5-7B-Instruct-AWQ (int4) · TP=1
3 HAMi slices × 12 GiB · 1 prefill + 2 decode · max_model_len 16,384

kv_bytes_per_token = 2 × 28 layers × 4 kv_heads × 128 head_dim × 2 B = 56 KiB
per slice          = 12 × 0.90 − 5.20 weights − 0.80 runtime = 4.80 GiB KV
one 16k sequence   = 0.875 GiB  →  5.5 seq/replica  →  decode pool 11.0
max_num_seqs = 64  →  KV binds
```

**int4 is forced, not chosen** — the A100 is sm80 and has no fp8 tensor cores, so
it is the only way a 7B fits three ways on 40 GiB. On a bigger card we would have
*declined* to quantize: freeing memory triples concurrency and leaves nothing for
admission to bind on.

| | At `max_model_len` | At the mean real request |
|---|---|---|
| KV per sequence | 0.875 GiB | 0.433 GiB |
| Sequences per replica | 5.5 | 11.1 |
| **Decode pool** | **11.0** | **22.2** |

A 16× spread between the two columns is the whole reason load level is the x-axis
of the experiment: admission matters in the first regime and not the second.

→ The six-step chain and every rejected alternative:
[`docs/project/01-capacity.md`](docs/project/01-capacity.md)

## Quick start

Needs a ≥40 GiB NVIDIA card, Docker with the NVIDIA runtime, and Python 3.11+.

```bash
git clone https://github.com/harshuljain13/Dodona.git && cd Dodona
cp .env.example .env            # fill in host, model, worker URLs, keys
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/
```

Then **stage 0** — no Kubernetes, one container, the whole card:

```bash
docker run -d --gpus all --ipc=host -p 8000:8000 vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --max-model-len 16384 --max-num-seqs 64 --gpu-memory-utilization 0.90
```

Four flags, all of them in the capacity arithmetic. Tool-calling flags arrive at
stage 9 with the agent — they buy nothing before there is a tool to call.

Its gate is four checks. Two of them decide whether anything else is worth doing:
**`config.json` must read 28 layers / 4 KV heads / `head_dim` 128** (= 56 KiB per
token), and **`nvidia-smi` must agree with the 5.20 GiB weights estimate.** If
either is off, redo the arithmetic before renting more time.

→ Every command in order: [**`COMMANDS.md`**](COMMANDS.md)
→ All ten stages and their gates: [`RUNBOOK.md`](RUNBOOK.md)

## Where to read what

| | |
|---|---|
| [**`PROJECT.md`**](PROJECT.md) | The problem in full, the decisions, the C4 architecture levels, alternatives rejected, risks |
| [**`SUBMISSION.md`**](SUBMISSION.md) | The design answered part by part, with a decision tree per part |
| [`docs/project/`](docs/README.md) | Capacity arithmetic, per-part depth, the run protocol, the results report |
| [`docs/cluster/`](docs/cluster/README.md) | The stack file by file — gateway, router, KV bus, observability |
| [`COMMANDS.md`](COMMANDS.md) | **Every command, in order**, marked `[Mac]` or `[GPU]` |
| [`RUNBOOK.md`](RUNBOOK.md) | The ten stages, their gates, and what each failure actually means |

```
app/        the voice agent — talks only to our serve path
control/    guard · admit · place · queue · hop · warm · stay-or-leave
cluster/    how the engine comes up
tests/      22 test files
notebook/   part5-queue.ipynb
metrics/    scrapes, decision logs, reports
assets/     logo, Grafana exhibits, charts
```

`control/` is the import root for three packages — `gateway`, `router`, `edge` —
carried over intact from the class-10 lab.

## Status

**Nothing has run on hardware.** Every number above is *predicted*, and eight
predictions are recorded to be checked later **without being edited**.

| | Stage | | | Stage |
|---|---|---|---|---|
| ○ | **0** vLLM on the bare box | | ○ | **6** Admission as a socket |
| ○ | **1** Open WebUI | | ○ | **7** Placement |
| ○ | **2** k3s + HAMi, three slices | | ○ | **8** KV hop + warm gate ◆ |
| ○ | **3** Prometheus + Grafana ◆ | | ○ | **9** The voice agent |
| ○ | **4** Gateway, pass-through | | ○ | **10** Measure |
| ○ | **5** Guardrails + tenant window | | | |

<sub>○ not started · ◆ one of the two stages that <b>must</b> exist</sub>

Stages **3** and **8** are the two that have to exist — a live `/metrics` scrape,
and a hop or a warmup proof. Stage 3 precedes the gateway deliberately: admission
is a *function of* engine metrics, so there is nothing to decide on until the
scrape exists.

## Contributing

Three conventions carry most of the weight:

- **Every number is marked predicted or measured**, and a prediction is never
  edited to match what happened. One wrong by 2× tells you which piece of
  arithmetic was misunderstood; one retrofitted teaches nothing.
- **One file owns each number.**
  [`docs/project/01-capacity.md`](docs/project/01-capacity.md) is the authority.
- **A claim needs an artifact.** "The policy fired" needs decision rows with a
  signal value. "The hop works" needs a non-empty `/hops` *and*
  `KV_BACKEND=mooncake`. No artifact means `TBM`.

```bash
pytest tests/ && python3 -m compileall -q control tests
```

`.claude/skills/` holds five skills that load automatically in Claude Code —
`dodona-stage`, `dodona-capacity`, `dodona-run`, `dodona-docs`, `dodona-review`.
They exist because this project has two characteristic ways of being wrong: a
number that no longer matches the hardware, and a claim no scrape supports.

## Built on

[AdmitPerf](https://github.com/harshuljain13/AdmitPerf) (policies, decision log,
report) · [AgentShip](https://github.com/Agent-Ship/agent-ship) (the agent and
its voice loop) ·
[llm-inference-experiments](https://github.com/harshuljain13/llm-inference-experiments)
(modules 1–10, where this cluster's shape comes from) ·
[vLLM](https://github.com/vllm-project/vllm) ·
[HAMi](https://github.com/Project-HAMi/HAMi) ·
[Mooncake](https://github.com/kvcache-ai/Mooncake)
