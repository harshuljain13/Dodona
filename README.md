<p align="center">
  <img src="assets/banner.svg" alt="Dodona" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/1%C3%97%20A100--40GB-0ea5e9?style=flat-square" alt="1x A100-40GB">
  <img src="https://img.shields.io/badge/Qwen2.5--7B--AWQ-f59e0b?style=flat-square" alt="Qwen2.5-7B-Instruct-AWQ">
  <img src="https://img.shields.io/badge/vLLM-7c3aed?style=flat-square" alt="vLLM">
  <img src="https://img.shields.io/badge/Deepgram-10b981?style=flat-square" alt="Deepgram">
  <img src="https://img.shields.io/badge/stage%200%20of%2010-64748b?style=flat-square" alt="stage 0 of 10">
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
  'lineColor':'#64748b','clusterBkg':'#fbfdff','clusterBorder':'#93c5fd',
  'edgeLabelBackground':'#ffffff'}}}%%
flowchart LR
    L(["Listener"])
    DG["Deepgram<br/>STT · TTS"]
    AG[["Research agent<br/>search · fetch · cite"]]

    subgraph CP["Control plane — what we write"]
        direction LR
        GD[/"guard"/] --> AD{"admit<br/>kv_pressure ≥ 0.90?"} --> PL["place"] --> HP["hop"]
    end

    RF(["503 · kv_free"])

    subgraph GPU["1 × A100-40GB — three HAMi slices"]
        direction LR
        PF["prefill ×1"] --> MC[("Mooncake")] --> DC["decode ×2<br/>5.5 seq each"]
    end

    L --> DG --> AG --> GD
    HP --> PF
    DC -->|"tokens"| AG --> DG --> L
    AD -->|"refused"| RF
    DC -. "kv_cache_usage_perc" .-> AD

    class L actor
    class DG,AG ext
    class GD,PL,HP ours
    class AD gate
    class PF,DC engine
    class MC store
    class RF dead

    classDef actor  fill:#fff1f2,stroke:#e11d48,stroke-width:2px,color:#881337
    classDef ext    fill:#faf5ff,stroke:#9333ea,stroke-width:2px,color:#581c87
    classDef ours   fill:#eff6ff,stroke:#2563eb,stroke-width:2px,color:#1e3a8a
    classDef gate   fill:#fffbeb,stroke:#d97706,stroke-width:2.5px,color:#78350f
    classDef engine fill:#f0fdf4,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef store  fill:#fff7ed,stroke:#ea580c,stroke-width:2px,color:#7c2d12
    classDef dead   fill:#fef2f2,stroke:#dc2626,stroke-width:2px,color:#7f1d1d
    linkStyle default stroke:#64748b,stroke-width:1.6px
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
