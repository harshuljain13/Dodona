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
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart TD
    subgraph CL["Clients"]
      L(["<b>Listener</b><br/>speaks"])
      DG["<b>Deepgram</b><br/>STT · TTS · off-GPU"]
      AG["<b>AgentShip</b> + LangGraph<br/>web_search · scrape_url · cite"]
      UI["<b>Open WebUI</b><br/>hand testing"]
      L --> DG
      DG <--> AG
    end

    subgraph GW["Control plane · <b>orch-serve</b>"]
      GD["<b>guard</b><br/>malformed → 400"]
      AD{"<b>admit</b><br/>kv_pressure ≥ 0.90?"}
      PL["<b>place</b><br/>least_loaded + queue depth"]
      HP["<b>hop</b><br/>src ≠ dst → transfer"]
      GD --> AD --> PL --> HP
      AP["<b>AdmitPerf</b><br/>policy · decision log"]
      RF(["<b>503</b> · kv_free"])
      OV{"<b>overflow gate</b><br/>503/529 may leave<br/>429 never does"}
      RP["<b>run report</b><br/>liveness verdict"]
    end


    subgraph K8S["<b>k3s</b> + <b>HAMi</b> · 1 × A100-40GB"]
      PF["<b>vLLM prefill-0</b><br/>slice 0 · 40% cores<br/>12k tok → 0.66 GiB KV"]
      MC[("<b>Mooncake</b><br/>KV store")]
      D0["<b>vLLM decode-0</b><br/>slice 1 · 28% cores<br/>4.80 GiB KV · 5.5 seq"]
      D1["<b>vLLM decode-1</b><br/>slice 2 · 28% cores<br/>4.80 GiB KV · 5.5 seq"]
      PF --> MC
      MC --> D0
      MC --> D1
      D0 ~~~ D1
    end

    SL["<b>Superlinked</b><br/>Qwen3.5-4B · 64 tok<br/>max 20 for the run"]

    subgraph OPS["Observability + autoscale"]
      PM[("<b>Prometheus</b><br/>+ DCGM exporter")]
      GF["<b>Grafana</b><br/>8 dashboards"]
      KD["<b>KEDA</b><br/>prefill-scaler on tokens-in-flight<br/>decode-scaler on kv_cache_usage"]
      PM --> GF
      PM --> KD
    end

    AG --> GD
    UI --> GD
    HP --> PF
    D0 --> AG
    D1 --> AG
    AD -->|"refused"| RF
    RF --> OV
    OV -->|"capacity · 503/529"| SL
    OV -.->|"tenant quota · 429"| AG
    SL --> AG
    AD <--> AP
    AP --> RP
    D0 -.->|"kv_cache_usage_perc"| AD
    PF --> PM
    D0 --> PM
    D1 --> PM
    KD -.->|"scales"| D0

    class L,RF actor
    class DG,AG,UI ext
    class GD,PL,HP ours
    class AD gate
    class PF,D0,D1 engine
    class MC store
    class AP lib
    class OV gate
    class SL overflow
    class PM,GF,KD obs
    class RP out

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

🔵 what we write · 🟢 vLLM, configured not touched · 🟠 KV storage ·
🟡 a decision · 🟣 clients and external services · ⚪ observability · 🔴 a refusal

**Three ways out of the control plane, and the difference is the design.**
Admitted work goes to the GPU. A capacity refusal (`503`/`529`) reaches the
overflow gate and *may* leave for Superlinked. A tenant over quota gets `429`
and **never leaves** — paying a provider for a request we are refusing on our
own policy grounds is overspending, not a capacity event.

**Two control loops, both dashed, both reading the same number.** `admit` takes
`kv_cache_usage_perc` straight off decode and **sheds** when it crosses 0.90 —
storing the value it read next to the verdict, which is the whole point. KEDA
takes the same metric through Prometheus and **scales** instead. Same signal,
opposite response.

The fleet stays at three pods for any measured run: a fleet that resizes
mid-comparison means the deployment moved, not the policy. KEDA is wired and
left at `min 1 · max 2` so the mechanism is real and visible.

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
| [**LEARNINGS.md**](LEARNINGS.md) | What went wrong during development and what each thing taught — the long version of the deployment-issues table |
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

Needs a ≥40 GiB NVIDIA card you can ssh to, and Python 3.11+ locally.

```bash
git clone https://github.com/harshuljain13/Dodona.git && cd Dodona
cp .env.example .env     # set LAMBDA and LAMBDA_SSH_KEY
make up
```

That is the whole thing. `make up` syncs the project to the box, builds the venv,
installs vLLM, starts the engine detached so it survives the ssh session, waits
for it to answer, **runs the four gate checks**, brings up Open WebUI, and leaves
you holding the tunnel. Then open **http://127.0.0.1:3000** and talk to it.

`make` on its own lists everything. **Three targets bring the stack up**, each
one further than the last:

| | | Stage |
|---|---|---|
| `make engine` | one worker on the whole card, Open WebUI, tunnel | 0–1 |
| `make cluster` | k3s + HAMi + **1 prefill + 2 decode** on three 12 GiB slices | 2 |
| `make observability` | Prometheus + Grafana + DCGM | 3 |
| `make all` | all three, in order | 0–3 |

All four run **from your Mac** and drive the box over ssh — no separate login.
Day to day: `make gate` · `make sync` · `make ssh` · `make logs` · `make down` ·
`make test` · `make lint`.

When one step fails and you want to redo just that step, `make ssh` and then the
`box-*` targets — `box-setup`, `box-serve`, `box-gate`, `box-cluster`,
`box-observability`, `box-webui`, `box-smoke`. They refuse to run on a machine
with no NVIDIA device and tell you the Mac-side equivalent.

The gate is the part worth watching. Two of its four checks decide whether
anything else is worth doing — `config.json` must read 28 layers / 4 KV heads /
`head_dim` 128 (**P6**, = 56 KiB per token), and the weights on the card must
match the 5.20 GiB estimate. **Every concurrency number in the project is
derived from those two.** If either fails, redo the arithmetic before renting
more time.

→ Every command individually, and what each failure means:
[**COMMANDS.md**](COMMANDS.md) · [**RUNBOOK.md**](RUNBOOK.md)

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
