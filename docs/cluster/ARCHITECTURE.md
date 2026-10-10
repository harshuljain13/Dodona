# Architecture — Module 10, cluster profiling

Overview only. Each component has its own deep dive; this page is the map.

Described with the [C4 model](https://c4model.com). Module 10 is the **complete
stack**: the disaggregated cluster, the observability layer, and the engine-tuning
tools, in one module.

| Layer | What it covers |
|---|---|
| Serving | Guardrails, admission, placement, prefill/decode split, KV hop, burst capacity |
| Observability | Per-replica metrics, stage histograms, ten Grafana dashboards, DCGM, load harnesses |
| Engine tuning | Attention HBM math, KV eviction policies, CUDA-graph warmup, pluggable KV transports |

Everything below the cluster steps runs on a laptop with no GPU.

## The docs

| Page | Covers |
|---|---|
| **[DATAFLOW.md](DATAFLOW.md)** | One request through the code, file by file — start here |
| [gateway.md](gateway.md) | Entry, guardrails, admission |
| [router.md](router.md) | Placement, scoring, shedding, the `router/` file map |
| [kvbus.md](kvbus.md) | Prefix bookkeeping, the KV hop, eviction |
| [observability.md](observability.md) | Histograms, `profile`, dashboard generation |
| [kernels.md](kernels.md) | Attention math, KV eviction, warmup, KV transports |
| [infrastructure.md](infrastructure.md) | SSH access, pod startup, HAMi, KEDA |
| [COMMANDS.md](COMMANDS.md) | Debugging reference |

---

## Level 1 — System context

```mermaid
flowchart LR
    OPERATOR["Lab operator<br/>[Person]"]
    CHATTER["Chat user<br/>[Person]"]

    SYS["<b>Module 10 cluster</b><br/>[Software System]<br/>Admits, places and serves inference;<br/>measures itself while doing it"]

    BURST["Burst capacity<br/>[External System]<br/>OpenAI-compatible endpoint"]
    HF["Hugging Face Hub<br/>[External System]<br/>Model weights"]

    OPERATOR -->|"drives load,<br/>reads dashboards"| SYS
    CHATTER -->|"chats in the browser"| SYS
    SYS -->|"503 / 529 only"| BURST
    SYS -->|"pulls weights at pod start"| HF
```

**Overflow lives at this level.** Burst capacity is the only external system the
request path can reach, and only on a capacity 503/529 — never on a 429 or a
`slice_oom`. That boundary is the system's most important architectural fact.

---

## Level 2 — Containers

Nine containers. C4 wants one container diagram; at nine boxes with a scrape
loop in the middle it becomes unreadable, so it is split by concern here — the
inventory table below is the complete list.

### 2a — Request path

```mermaid
flowchart LR
    CHATTER["Chat user"]
    LOAD["Locust / crew_flood<br/>[on your Mac]"]

    subgraph CLUSTER["k3s cluster"]
        WEBUI["<b>open-webui</b><br/>[Container: NodePort 30030]"]
        ORCH["<b>orch-serve</b><br/>[Container: Python :8080]<br/>guardrails · admission · routing"]
        PRE["<b>vllm-prefill</b><br/>[Container: vLLM :8000]<br/>text model"]
        DEC["<b>vllm-decode</b><br/>[Container: vLLM :8000]<br/>vision model"]
        MOON["<b>mooncake-store</b><br/>[Container: :50051]<br/>KV hop records"]
    end

    BURST["<b>Burst capacity</b><br/>[External System]"]

    CHATTER --> WEBUI
    WEBUI -->|"OPENAI_API_BASE_URL"| ORCH
    LOAD -->|"via SSH tunnel"| ORCH
    ORCH -->|"chat completions"| PRE
    ORCH -->|"chat completions"| DEC
    ORCH -->|"POST /put"| MOON
    ORCH -.->|"503 / 529 only"| BURST
```

### 2b — Control and observability

```mermaid
flowchart LR
    subgraph SCRAPED["scrape targets"]
        ORCH2["orch-serve :8080"]
        VLLM2["vllm-prefill / vllm-decode :8000"]
        MOON2["mooncake-store :50051"]
        DCGM["<b>dcgm-exporter</b><br/>[Container: DaemonSet]<br/>GPU telemetry"]
    end

    PROM["<b>prometheus-server</b><br/>[Container: monitoring ns]"]
    GRAF["<b>grafana</b><br/>[Container: NodePort]<br/>ten Class 10 dashboards"]
    KEDA["<b>KEDA operator</b><br/>[Container: keda ns]<br/>metric → replica count"]
    SCALE["replica count on<br/>vllm-prefill / vllm-decode"]

    ORCH2 --> PROM
    VLLM2 --> PROM
    MOON2 --> PROM
    DCGM --> PROM
    PROM -->|"dashboard queries"| GRAF
    PROM -->|"trigger query"| KEDA
    KEDA -->|"scales"| SCALE
```

`KEDA → replica count` feeds back into the pods in 2a — that loop is drawn
open here rather than closed so the layout stays readable. See
[infrastructure.md](infrastructure.md#the-autoscale-loop) for the timing.

### Container inventory

| Container | Type | Where | Covered in |
|---|---|---|---|
| `open-webui` | Browser chat, NodePort 30030 | `default` | [infrastructure.md](infrastructure.md) |
| `orch-serve` | Gateway + router, :8080 | `default` | [gateway.md](gateway.md), [router.md](router.md) |
| `vllm-prefill` | vLLM engine, text model | `default` | [DATAFLOW.md](DATAFLOW.md#8-engine-internals) |
| `vllm-decode` | vLLM engine, vision model | `default` | [DATAFLOW.md](DATAFLOW.md#8-engine-internals) |
| `mooncake-store` | KV hop record server | `default` | [kvbus.md](kvbus.md) |
| `prometheus-server` | Scrapes all of the above | `monitoring` | [observability.md](observability.md) |
| `grafana` | Ten dashboards | `monitoring` | [observability.md](observability.md) |
| `dcgm-exporter` | GPU telemetry DaemonSet | `monitoring` | [observability.md](observability.md) |
| `keda-operator` | Autoscaling controller | `keda` | [infrastructure.md](infrastructure.md) |

`hami-scheduler` (`kube-system`) is deliberately absent — it schedules pods onto
GPU slices but is not a container in the data path. It appears in the
[deployment view](#deployment-view) instead, which is where C4 puts
infrastructure.

---

## Level 3 — Components inside orch-serve

```mermaid
flowchart LR
    subgraph ORCH["orch-serve"]
        H["<b>Handler</b><br/>[Component: gateway/serve.py]"]
        GR["<b>guardrails</b><br/>[Component: app/guardrails.py]"]
        OF["<b>Overflow</b><br/>[Component: router/overflow.py]<br/>local-first, bursts on 503/529"]
        AD["<b>Gateway</b><br/>[Component: gateway/admission.py]<br/>per-tenant cap"]
        RT["<b>Router</b><br/>[Component: router/router.py]<br/>filter → deadline → score"]
        QU["<b>enqueue</b><br/>[Component: gateway/queue.py]<br/>sequences the two phases"]
        KV["<b>KVBus</b><br/>[Component: router/kvbus.py]"]
        PO["<b>Worker adapters</b><br/>[Component: router/pools.py]"]
        MT["<b>METRICS</b><br/>[Component: gateway/metrics.py]"]
        PL["<b>planner</b><br/>[Component: router/planner.py]<br/>print-only"]
    end

    H --> GR
    GR --> OF
    OF --> AD
    AD --> RT
    RT --> QU
    QU --> PO
    QU --> KV
    OF -.-> MT
    AD -.-> MT
    RT -.-> MT
    PL -.-> MT
```

| Component | Deep dive |
|---|---|
| `Handler`, `guardrails`, `Gateway` | [gateway.md](gateway.md) |
| `Router`, `Overflow`, `Worker adapters` | [router.md](router.md) |
| `KVBus`, `enqueue` | [kvbus.md](kvbus.md), [DATAFLOW.md](DATAFLOW.md#4-the-queue--there-isnt-one) |
| `METRICS`, `planner` | [observability.md](observability.md) |

---

## Deployment view

Where HAMi sits: it is infrastructure, not a container in the request path.

```mermaid
flowchart LR
    subgraph MAC["Mac [Deployment Node]"]
        TUN["ssh -L x7<br/>Locust · crew_flood · REPL"]
    end

    subgraph BOX["Lambda GPU box [Deployment Node]"]
        subgraph K3S["k3s"]
            SCHED["<b>hami-scheduler</b><br/>[kube-system]<br/>binds pods to GPU slices"]
            PODS["all nine containers"]
        end
        GPU["Physical GPU<br/>sliced into gpumem / gpucores shares"]
    end

    TUN --> PODS
    SCHED -->|"admits by gpumem"| PODS
    PODS -->|"vLLM pods claim a slice"| GPU
```

HAMi's CUDA shim makes each vLLM container see **its slice, not the card** —
which is why `--gpu-memory-utilization` is a fraction of the slice. Full
explanation in [DATAFLOW.md](DATAFLOW.md#6-hami--what-it-buys-you).

---

## Where things are *not*

Worth stating up front, because the file layout implies otherwise:

- **No queue.** `gateway/queue.py` sequences two phases; it holds nothing. See
  [DATAFLOW.md](DATAFLOW.md#4-the-queue--there-isnt-one).
- **No parallelism.** Every engine is world size 1. See
  [DATAFLOW.md](DATAFLOW.md#9-parallelism--there-isnt-any).
- **No laptop path.** Module 9's `fakeworker` does not exist in 10.
- **`planner.py` actuates nothing** — KEDA does the scaling.
