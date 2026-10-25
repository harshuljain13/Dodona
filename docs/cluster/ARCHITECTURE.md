# Architecture — The upstream lab, cluster profiling

Overview only. Each component has its own deep dive; this page is the map.

Described with the [C4 model](https://c4model.com). The upstream lab is the **complete
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
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart LR
    OPERATOR["Lab operator<br/>[Person]"]
    CHATTER["Chat user<br/>[Person]"]

    SYS["The upstream lab cluster<br/>[Software System]"]

    BURST["Burst capacity<br/>[External System]"]
    HF["Hugging Face Hub<br/>[External System]"]

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
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart LR
    CHATTER["Chat user"]
    LOAD["Locust / crew_flood<br/>[on your Mac]"]

    subgraph CLUSTER["k3s cluster"]
        WEBUI["open-webui<br/>[Container: NodePort 30030]"]
        ORCH["orch-serve<br/>[Container: Python :8080]"]
        PRE["vllm-prefill<br/>[Container: vLLM :8000]"]
        DEC["vllm-decode<br/>[Container: vLLM :8000]"]
        MOON["mooncake-store<br/>[Container: :50051]"]
    end

    BURST["Burst capacity<br/>[External System]"]

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
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart LR
    subgraph SCRAPED["scrape targets"]
        ORCH2["orch-serve :8080"]
        VLLM2["vllm-prefill / vllm-decode :8000"]
        MOON2["mooncake-store :50051"]
        DCGM["dcgm-exporter<br/>[Container: DaemonSet]"]
    end

    PROM["prometheus-server<br/>[Container: monitoring ns]"]
    GRAF["grafana<br/>[Container: NodePort]"]
    KEDA["KEDA operator<br/>[Container: keda ns]"]
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
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart LR
    subgraph ORCH["orch-serve"]
        H["Handler<br/>[Component: gateway/serve.py]"]
        GR["guardrails<br/>[Component: app/guardrails.py]"]
        OF["Overflow<br/>[Component: router/overflow.py]"]
        AD["Gateway<br/>[Component: gateway/admission.py]"]
        RT["Router<br/>[Component: router/router.py]"]
        QU["enqueue<br/>[Component: gateway/queue.py]"]
        KV["KVBus<br/>[Component: router/kvbus.py]"]
        PO["Worker adapters<br/>[Component: router/pools.py]"]
        MT["METRICS<br/>[Component: gateway/metrics.py]"]
        PL["planner<br/>[Component: router/planner.py]"]
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
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart LR
    subgraph MAC["Mac [Deployment Node]"]
        TUN["ssh -L x7<br/>Locust · crew_flood · REPL"]
    end

    subgraph BOX["GPU box [Deployment Node]"]
        subgraph K3S["k3s"]
            SCHED["hami-scheduler<br/>[kube-system]"]
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
