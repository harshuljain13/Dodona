<p align="center">
  <img src="assets/banner.svg" alt="Dodona" width="100%">
</p>

<p align="center">
  <a href="https://github.com/vllm-project/vllm"><img src="https://img.shields.io/badge/vLLM-30A2FF?style=flat-square&logoColor=white" alt="vLLM"></a>
  <a href="https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-AWQ"><img src="https://img.shields.io/badge/Qwen2.5-615CED?style=flat-square" alt="Qwen2.5"></a>
  <a href="https://github.com/Project-HAMi/HAMi"><img src="https://img.shields.io/badge/HAMi-00B0D7?style=flat-square" alt="HAMi"></a>
  <a href="https://github.com/kvcache-ai/Mooncake"><img src="https://img.shields.io/badge/Mooncake-F59E0B?style=flat-square" alt="Mooncake"></a>
  <a href="https://k3s.io"><img src="https://img.shields.io/badge/k3s-326CE5?style=flat-square&logo=kubernetes&logoColor=white" alt="k3s"></a>
  <a href="https://prometheus.io"><img src="https://img.shields.io/badge/Prometheus-E6522C?style=flat-square&logo=prometheus&logoColor=white" alt="Prometheus"></a>
  <a href="https://grafana.com"><img src="https://img.shields.io/badge/Grafana-F46800?style=flat-square&logo=grafana&logoColor=white" alt="Grafana"></a>
  <a href="https://github.com/harshuljain13/AdmitPerf"><img src="https://img.shields.io/badge/AdmitPerf-F5C518?style=flat-square&labelColor=0B0B0B" alt="AdmitPerf"></a>
  <a href="https://github.com/Agent-Ship/agent-ship"><img src="https://img.shields.io/badge/AgentShip-0891B2?style=flat-square" alt="AgentShip"></a>
  <a href="https://github.com/langchain-ai/langgraph"><img src="https://img.shields.io/badge/LangGraph-1C3C3C?style=flat-square" alt="LangGraph"></a>
  <a href="https://deepgram.com"><img src="https://img.shields.io/badge/Deepgram-13B981?style=flat-square" alt="Deepgram"></a>
  <a href="https://superlinked.com"><img src="https://img.shields.io/badge/Superlinked-6366F1?style=flat-square" alt="Superlinked"></a>
  <a href="https://lambdalabs.com"><img src="https://img.shields.io/badge/Lambda-111827?style=flat-square" alt="Lambda"></a>
  <img src="https://img.shields.io/badge/Python%203.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+">
</p>

---

**Ask aloud. Dodona searches, reads the sources, and answers out loud with
citations — entirely on one GPU you rent.** No hosted chat API in the answer path.

The agent is the easy half. One fetched source arrives as a **12,000-token
prefill holding 0.66 GiB of KV**; a single A100 split three ways serves
**11 concurrent sequences**; and a listener is waiting out loud, so latency is
dead air rather than a percentile. The cluster has to decide what to let in —
and most deployments make that decision by accident, when a queue fills and
everything slows together.

Dodona makes it on purpose, and writes down what it saw.

> *The oracle at Dodona answered questions by reading the sound of wind through
> the oak leaves.* Voice in, sourced answer out.

## Overview

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'14px',
  'lineColor':'#475569','clusterBkg':'#fcfdff','clusterBorder':'#94a3b8',
  'edgeLabelBackground':'#ffffff'}}}%%
flowchart TD
    L(["<b>Listener</b><br/>speaks a question"])
    DG["<b>Deepgram</b><br/>STT · TTS · off-GPU"]
    AG["<b>AgentShip</b><br/>LangGraph research agent<br/>web_search · scrape_url · cite"]

    subgraph CP["<b>control/</b> — what we write"]
        GD["<b>guard</b><br/>malformed → 400"]
        AD{"<b>admit</b><br/>kv_pressure ≥ 0.90?"}
        PL["<b>place</b><br/>least_loaded + queue depth"]
        HP["<b>hop</b><br/>prefill → decode"]
        GD --> AD --> PL --> HP
    end

    AP["<b>AdmitPerf</b><br/>policy + decision log<br/><i>records the value it read</i>"]
    RF(["<b>503</b> · kv_free<br/>spoken, not dead air"])

    subgraph GPU["<b>1 × A100-40GB</b> · 3 slices"]
        PF["<b>vLLM prefill ×1</b><br/>12k tok → 0.66 GiB KV<br/>builds, then frees"]
        MC[("<b>Mooncake</b><br/>KV store")]
        DC["<b>vLLM decode ×2</b><br/>4.80 GiB KV each<br/>5.5 seq → pool 11"]
        PF --> MC --> DC
    end

    OB[("<b>Prometheus + Grafana</b>")]
    RP(["<b>run report</b><br/>SIGNAL LIVENESS: ?"])

    L --> DG --> AG --> GD
    HP --> PF
    DC -->|"tokens"| AG
    AG --> DG
    AD -->|"refused"| RF
    AD <--> AP
    AP --> RP
    DC -.->|"live engine state"| AD
    PF --> OB
    DC --> OB

    class L,RF actor
    class DG,AG ext
    class GD,PL,HP ours
    class AD gate
    class PF,DC engine
    class MC store
    class AP lib
    class OB obs
    class RP out

    classDef actor  fill:#ffe4e6,stroke:#be123c,stroke-width:2px,color:#881337
    classDef ext    fill:#f3e8ff,stroke:#7e22ce,stroke-width:2px,color:#581c87
    classDef ours   fill:#dbeafe,stroke:#1d4ed8,stroke-width:2px,color:#1e3a8a
    classDef gate   fill:#fef3c7,stroke:#b45309,stroke-width:3px,color:#78350f
    classDef engine fill:#dcfce7,stroke:#15803d,stroke-width:2px,color:#14532d
    classDef store  fill:#ffedd5,stroke:#c2410c,stroke-width:2px,color:#7c2d12
    classDef lib    fill:#e0e7ff,stroke:#4338ca,stroke-width:2px,color:#312e81
    classDef obs    fill:#f1f5f9,stroke:#475569,stroke-width:2px,color:#1e293b
    classDef out    fill:#ccfbf1,stroke:#0f766e,stroke-width:2.5px,color:#134e4a
    linkStyle default stroke:#475569,stroke-width:1.7px
```

🔵 what we write · 🟢 the engine, configured not touched · 🟠 KV storage ·
🟡 the decision · 🟣 off-GPU · 🔴 a refusal

The dashed edge is the one that matters: `admit` reads live KV pressure off the
decode pool, so its verdict depends on what the workers are actually doing — and
the value it read is stored next to the verdict.

```
1 × A100-40GB · Qwen2.5-7B-Instruct-AWQ (int4) · TP=1
3 HAMi slices × 12 GiB · 1 prefill + 2 decode · max_model_len 16,384
→ 4.80 GiB of KV per worker · 5.5 sequences each · decode pool 11
```

## Documentation

| | |
|---|---|
| [**PROJECT.md**](PROJECT.md) | The problem in full, the decision behind every choice, the C4 architecture levels, alternatives rejected, risks |
| [**SUBMISSION.md**](SUBMISSION.md) | The design answered part by part, with a decision tree per part |
| [**COMMANDS.md**](COMMANDS.md) | Every command in order, marked `[Mac]` or `[GPU]` |
| [**RUNBOOK.md**](RUNBOOK.md) | The ten stages, their gates, and what each failure actually means |
| [`docs/project/`](docs/README.md) | Capacity arithmetic, per-part depth, the run protocol, the results report |
| [`docs/cluster/`](docs/cluster/README.md) | The stack file by file — gateway, router, KV bus, observability |

```
app/        the voice agent — talks only to our serve path
control/    guard · admit · place · queue · hop · warm · stay-or-leave
cluster/    how the engine comes up
docs/       project-level and cluster-level deep dives
tests/      22 test files
notebook/   part5-queue.ipynb
metrics/    scrapes, decision logs, reports
```

## Quick start

Needs a ≥40 GiB NVIDIA card, Docker with the NVIDIA runtime, and Python 3.11+.

```bash
git clone https://github.com/harshuljain13/Dodona.git && cd Dodona
cp .env.example .env                    # set LAMBDA and LAMBDA_SSH_KEY
bash cluster/setup/sync_to_lambda.sh    # ship it to the box
bash cluster/setup/ssh.sh               # tunnel + shell, leave it open
```

Then on the box:

```bash
bash cluster/setup/lambda_setup.sh      # venv, requirements, vllm, verify
bash cluster/setup/lambda_vllm.sh       # serve on :8000
```

**[COMMANDS.md](COMMANDS.md)** has the rest, including the gate that must pass
before stage 1: `config.json` has to read 28 layers / 4 KV heads / `head_dim` 128,
because every concurrency number in the project is derived from it.

## Status

**Nothing has run on hardware.** Every number here is *predicted*, and eight
predictions are written down to be checked later **without being edited**.

Stages **3** (a live `/metrics` scrape) and **8** (a hop or a warmup proof) are
the two that must exist. Stage 3 comes before the gateway deliberately —
admission is a *function of* engine metrics, so there is nothing to decide on
until the scrape exists. Progress: [RUNBOOK.md](RUNBOOK.md).

## Contributing

Three conventions carry most of the weight.

**Every number is marked predicted or measured**, and a prediction is never
edited to match what happened. One wrong by 2× tells you which piece of
arithmetic was misunderstood; one quietly retrofitted teaches nothing.

**One file owns each number.**
[`docs/project/01-capacity.md`](docs/project/01-capacity.md) is the authority —
everything else follows it, never the reverse.

**A claim needs an artifact.** "The policy fired" needs decision rows carrying a
signal value. "The hop works" needs a non-empty `/hops` *and*
`KV_BACKEND=mooncake` confirmed. No artifact means `TBM`.

```bash
pytest tests/ && python3 -m compileall -q control tests
```

`.claude/skills/` holds five skills that load automatically in Claude Code —
`dodona-stage`, `dodona-capacity`, `dodona-run`, `dodona-docs`, `dodona-review`.
They exist because this project has two characteristic ways of being wrong: a
number that no longer matches the hardware, and a claim no scrape supports.

## Built on

[vLLM](https://github.com/vllm-project/vllm) ·
[HAMi](https://github.com/Project-HAMi/HAMi) ·
[Mooncake](https://github.com/kvcache-ai/Mooncake) ·
[AdmitPerf](https://github.com/harshuljain13/AdmitPerf) ·
[AgentShip](https://github.com/Agent-Ship/agent-ship) ·
[Deepgram](https://deepgram.com) ·
[Lambda](https://lambdalabs.com) ·
[llm-inference-experiments](https://github.com/harshuljain13/llm-inference-experiments)
