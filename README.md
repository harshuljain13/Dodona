<p align="center">
  <img src="assets/banner.svg" alt="Dodona" width="100%">
</p>

<p align="center">
  <a href="https://github.com/vllm-project/vllm"><img src="https://img.shields.io/badge/vLLM-30A2FF?style=flat-square&logoColor=white" alt="vLLM"></a>
  <a href="https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-AWQ"><img src="https://img.shields.io/badge/Qwen2.5-615CED?style=flat-square" alt="Qwen2.5"></a>
  <a href="https://prometheus.io"><img src="https://img.shields.io/badge/Prometheus-E6522C?style=flat-square&logo=prometheus&logoColor=white" alt="Prometheus"></a>
  <a href="https://grafana.com"><img src="https://img.shields.io/badge/Grafana-F46800?style=flat-square&logo=grafana&logoColor=white" alt="Grafana"></a>
  <a href="https://github.com/harshuljain13/AdmitPerf"><img src="https://img.shields.io/badge/AdmitPerf-F5C518?style=flat-square&labelColor=0B0B0B" alt="AdmitPerf"></a>
  <a href="https://github.com/Agent-Ship/agent-ship"><img src="https://img.shields.io/badge/AgentShip-0891B2?style=flat-square" alt="AgentShip"></a>
  <a href="https://github.com/langchain-ai/langgraph"><img src="https://img.shields.io/badge/LangGraph-1C3C3C?style=flat-square" alt="LangGraph"></a>
  <a href="https://deepgram.com"><img src="https://img.shields.io/badge/Deepgram-13B981?style=flat-square" alt="Deepgram"></a>
  <a href="https://superlinked.com"><img src="https://img.shields.io/badge/Superlinked-6366F1?style=flat-square" alt="Superlinked"></a>
  <img src="https://img.shields.io/badge/Python%203.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+">
</p>

---

**Ask aloud. Dodona searches, reads the sources, and answers out loud with
citations — entirely on one GPU you rent.** No hosted chat API in the answer path.

The agent is the easy half. One fetched source arrives as a **12,000-token
prefill**, a rented box holds a fixed and knowable amount of KV, and a listener
is waiting out loud — so latency is dead air rather than a percentile. The
cluster has to decide what to let in, and most deployments make that decision by
accident, when a queue fills and everything slows together.

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
    subgraph CL["The app · all of it on this laptop"]
      direction TB
      L(["<b>Listener</b><br/>speaks"])
      WEB["<b>Web UI</b> · Next.js :3000<br/>mic → 24 kHz PCM, one socket"]
      subgraph AS["<b>AgentShip</b> · :8090 · one process"]
        direction TB
        VC["<b>voice cascade</b><br/>stt · vad · tts<br/><i>built from the spec</i>"]
        AG["<b>Dodona agent</b> · LangGraph<br/>web_search · scrape_url"]
        VC <--> AG
      end
      L -->|"mic"| WEB
      WEB <-->|"/voice"| VC
    end

    subgraph EXT["Off this machine · not the GPU"]
      direction TB
      DG["<b>Deepgram</b><br/>nova-3 · aura-2"]
      WB["<b>Firecrawl</b><br/>what the tools read"]
    end

    VC <-->|"speech ⇄ text"| DG
    AG <-->|"search · scrape"| WB

    subgraph GW["Control plane · <b>gateway</b> :8080 · one process"]
      GD["<b>guard</b><br/>unknown model, bad max_tokens → 400"]
      AD{"<b>admit</b><br/>kv_cache_usage ≥ 0.90?"}
      PL["<b>place</b><br/>least-loaded + queue depth<br/>+ who holds this prefix"]
      GD --> AD --> PL
      AP["<b>AdmitPerf</b><br/>policy · decision log"]
      IX[("<b>prefix index</b><br/>which replica holds which prompt")]
      RF(["<b>503</b> · no kv_free"])
      OV{"<b>overflow gate</b><br/>503/529 may leave<br/>429 never does"}
      RP["<b>run report</b><br/>liveness verdict"]
    end

    subgraph FLEET["<b>The fleet</b> · data parallel · one engine per card"]
      D0["<b>vLLM engine-0</b><br/>whole card · prefill AND decode"]
      D1["<b>vLLM engine-1</b><br/>whole card · prefill AND decode"]
      D0 ~~~ D1
    end

    SL["<b>Superlinked</b><br/>Qwen3.5-4B · 64 tok<br/>max 20 for the run"]

    subgraph OPS["Observability"]
      PM[("<b>Prometheus</b><br/>+ GPU exporter")]
      GF["<b>Grafana</b><br/>4 dashboards, in request order"]
      PM --> GF
    end

    AG --> GD
    PL --> D0
    PL --> D1
    PL <-.->|"prefix hit → send it back"| IX
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
    D1 -.->|"kv_cache_usage_perc"| AD
    D0 --> PM
    D1 --> PM

    class L,RF actor
    class DG,WB ext
    class GD,PL,WEB ours
    class AD gate
    class D0,D1 engine
    class IX store
    class AP,AG,VC lib
    class OV gate
    class SL overflow
    class PM,GF obs
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

**One control loop, and it is the point of the project.** `admit` reads
`kv_cache_usage_perc` off the engines and **sheds** when it crosses 0.90,
storing the value it read next to the verdict. A refusal you can explain is
worth more than a queue that silently grows.

**Not autoscaling.** Scaling takes minutes to load weights; KV pressure moves in
seconds. That gap is the argument *for* admission control, and it is taught with
the number rather than demonstrated with a scaler that cannot win the race.
[`docs/PROJECT.md`](docs/PROJECT.md) has the reasoning.

```
2 × A100-80GB · one vLLM per card · data parallel · TP=1
Qwen2.5-32B-Instruct-AWQ (int4) · max_model_len 16,384
```

Both replicas serve both phases, so `place()` is choosing where work goes rather
than which phase goes where. The concurrency that follows depends on the card and
on `SLICE_MIB`, so it is measured rather than quoted — see
[`docs/project/01-capacity.md`](docs/project/01-capacity.md) for the arithmetic
and [`benchmarking/04-routing-admission/`](benchmarking/04-routing-admission/)
for the notebook that measures it on your box.

**The number that decides whether a run means anything.** The policy fires at
0.90 KV, so the KV pool has to be fillable by load you can actually generate.
Give a 32B the whole 80 GiB card and the pool is ~217k tokens — more than
`MAX_NUM_SEQS` can hold at once, so the signal never arrives and the run comes
back **INERT**: numbers indistinguishable from having no policy at all.
`SLICE_MIB` shrinks the pool until the threshold is reachable. Notebook 04
checks this before it generates any load, because it is the one mistake that
invalidates everything downstream.

### How it runs

```
make fleet-runpod            one vLLM per card, the gateway in front
make fleet-runpod-ssh        the tunnel — leave it open
make fleet-runpod-status     model, KV pressure, placement, uptime
make fleet-runpod-smoke-test does a request come back, with tools
make fleet-runpod-down       stop it
```

It runs in a **container**, which is what most rented GPUs are — so there is no
k3s, no HAMi and no KEDA on this branch, and nothing here needs systemd to be
PID 1.

### Other topologies live on their own branches

Each is a real variant with its own trade-offs, kept separate so that `main` is
one architecture explained honestly rather than four behind feature flags:

| branch | what it is |
|---|---|
| `main` | **this** — RunPod, data-parallel replicas, admission control and routing |
| [`lambda-cluster`](https://github.com/harshuljain13/Dodona/tree/lambda-cluster) | k3s + HAMi + KEDA on one card sliced three ways. Needs a host, not a container |
| [`lambda-pd-disaggregation`](https://github.com/harshuljain13/Dodona/tree/lambda-pd-disaggregation) | prefill/decode split with vLLM's KV connector. Needs RDMA to be worth it — see [`docs/project/topology.md`](docs/project/topology.md) |
| [`runpod-tp-cluster`](https://github.com/harshuljain13/Dodona/tree/runpod-tp-cluster) | one model sharded across cards with tensor parallel, for a model that will not fit on one |

Everything between the listener and the engines is the same on every branch —
the gateway, guard, admission policy, placement, queue, dashboards and reports.
What differs is how the workers get started and what enforces their memory.

## Documentation

| | |
|---|---|
| [**PREREQUISITES.md**](PREREQUISITES.md) | Accounts, keys, the SSH key and the GPU box — numbered steps, done once before `make fleet-runpod` |
| [**PROJECT.md**](docs/PROJECT.md) | The problem in full, the decision behind every choice, the C4 architecture levels, alternatives rejected, risks |
| [**SUBMISSION.md**](docs/SUBMISSION.md) | The design answered part by part, with a decision tree per part |
| [**COMMANDS.md**](docs/COMMANDS.md) | Every command in order, marked `[Mac]` or `[GPU]` |
| [**RUNBOOK.md**](docs/RUNBOOK.md) | The bring-up in order, the gate at each step, and what each failure actually means |
| [`docs/project/`](docs/README.md) | Capacity arithmetic, per-part depth, the run protocol, the results report |
| [`docs/cluster/`](docs/cluster/README.md) | The stack file by file — gateway, router, KV bus, observability |

```
app/                 the voice agent — talks only to our serve path
benchmarking/        everything that measures the cluster
  load.py            agent-shaped load; run it ON THE BOX, not over the tunnel
  locustfile.py      the Locust UI, for watching rather than measuring
  reports/           generated run reports
  notebook/          part5-queue.ipynb
infra/               everything that runs the cluster
  control/           guard · admit · place · queue · hop · warm · overflow
  cluster/           setup/, k8s/, observability/, kernels/
  metrics/           traces and load bundles
docs/
  PROJECT.md         the problem, every decision, the C4 levels
  SUBMISSION.md      the design answered part by part
  RUNBOOK.md         the bring-up in order, with a gate at each step
  COMMANDS.md        every command, marked [Mac] or [GPU]
  project/ cluster/  deep dives
tests/               22 test files
```

`infra/` runs the cluster, `docs/` explains it, `app/` is the agent. The repo
root holds this README and the Makefile, nothing else.

## Quick start

Needs **two NVIDIA cards** you can ssh to — two so that `place()` is choosing
between replicas rather than routing to the only one — and Python 3.11+ locally.
Accounts, keys and the box, in order, are
[**PREREQUISITES.md**](PREREQUISITES.md) — about twenty minutes, once.

```bash
git clone https://github.com/harshuljain13/Dodona.git && cd Dodona
cp .env.example .env     # set BOX, SSH_KEY, SSH_PORT — see PREREQUISITES.md
make doctor              # is .env complete?

make fleet-runpod        # ONE command: sync, install, start, tunnel
```

It syncs, installs vLLM, starts the fleet **detached** on the box, waits for
every component to answer, then opens the tunnel in your terminal. One command,
one terminal, and safe to re-run — it stops a running fleet first rather than
silently doing nothing. `make fleet-runpod-down` stops it,
`make fleet-runpod-logs` follows it.

**Four commands, and no more.** All run from your Mac and drive the box over
ssh — there is no separate login step.

| | |
|---|---|
| `make doctor` | **start here.** Is `.env` complete? Names every value and what its absence breaks, printing no secrets |
| `make fleet-runpod` | syncs, installs vLLM, starts one engine per card with the gateway in front, Prometheus and Grafana beside it, then opens the tunnel. Safe to re-run: it stops a running fleet first |
| `make app` | the voice app — the AgentShip service and the UI on `localhost:3000`. It reaches the GPU through the tunnel, so bring the fleet up first |
| `make test` | the control plane, locally, **with no GPU** |

Stopping, status, logs, the smoke test, load generation and the one-off tools
are in [COMMANDS.md](docs/COMMANDS.md).

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
[**COMMANDS.md**](docs/COMMANDS.md) · [**RUNBOOK.md**](docs/RUNBOOK.md)

## Status

**Nothing has run on hardware.** Every number here is *predicted*, and eight
predictions are written down to be checked later **without being edited**.

Stages **3** (a live `/metrics` scrape) and **8** (a hop or a warmup proof) are
the two that must exist. Observability comes before the gateway deliberately —
admission is a *function of* engine metrics, so there is nothing to decide on
until the scrape exists. Progress: [RUNBOOK.md](docs/RUNBOOK.md).

## Contributing

Three conventions carry most of the weight.

**Every number is marked predicted or measured**, and a prediction is never
edited to match what happened. One wrong by 2× tells you which piece of
arithmetic was misunderstood; one quietly retrofitted teaches nothing.

**One file owns each number.**
[`docs/project/01-capacity.md`](docs/project/01-capacity.md) is the authority —
everything else follows it, never the reverse.

**A claim needs an artifact.** "The policy fired" needs decision rows carrying
the signal value it read. "Placement is a choice" needs two replicas with
traffic on both. "Reuse saved a prefill" needs cached prompt tokens, not a
hit-rate gauge read once. No artifact means `TBM`.

**And a claim needs to be about this branch.** The honesty that cost the most
here: a prefill/decode split records a *decision* in a ledger, while moving the
bytes happens inside vLLM behind `--kv-transfer-config`. Miss that and the split
is worse than not splitting, because decode re-prefills and one answer costs two
prefills. `main` does not claim a hop, and
[`docs/project/topology.md`](docs/project/topology.md) works through why.

```bash
pytest tests/ && python3 -m compileall -q control tests
```

`.claude/skills/` holds five skills that load automatically in Claude Code —
`dodona-bringup`, `dodona-capacity`, `dodona-run`, `dodona-docs`, `dodona-review`.
They exist because this project has two characteristic ways of being wrong: a
number that no longer matches the hardware, and a claim no scrape supports.

## Built on

[vLLM](https://github.com/vllm-project/vllm) ·
[AdmitPerf](https://github.com/harshuljain13/AdmitPerf) ·
[AgentShip](https://github.com/Agent-Ship/agent-ship) ·
[Deepgram](https://deepgram.com) ·
[Lambda](https://lambdalabs.com) ·
[RunPod](https://runpod.io) ·
[llm-inference-experiments](https://github.com/harshuljain13/llm-inference-experiments)
