# Observability — metrics, profiling and dashboards

The histograms 10 adds, and how the ten Grafana dashboards are generated.

Part of the [module 10 docs](README.md). Overview: [ARCHITECTURE.md](ARCHITECTURE.md) · Request path: [DATAFLOW.md](DATAFLOW.md)

---

## The observation path

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'fontFamily':'ui-sans-serif, -apple-system, Segoe UI, Inter, Helvetica, Arial, sans-serif','fontSize':'14px',
  'primaryColor':'#ecfdf5','primaryTextColor':'#0f172a','primaryBorderColor':'#059669',
  'secondaryColor':'#f8fafc','tertiaryColor':'#ffffff',
  'lineColor':'#94a3b8','textColor':'#1e293b',
  'clusterBkg':'#ffffff','clusterBorder':'#a7f3d0',
  'edgeLabelBackground':'#ffffff','nodeBorder':'#059669'
}}}%%
flowchart LR
    ROUTER["Router.pick<br/>router/router.py"]
    METRICS["METRICS<br/>gateway/metrics.py"]
    HIST["Histogram<br/>per stage"]
    RENDER["render<br/>Prometheus text on /metrics"]

    ROUTER -->|"observe_duration('pick', dt)"| METRICS
    METRICS --> HIST
    HIST --> RENDER
```

Module 9's metrics were counters and gauges. 10 adds **histograms**, which is
what makes `profile` possible.

| Stage | Measures |
|---|---|
| `gateway` | Admission — the tenant cap check |
| `pick` | Placement — filter, deadline, sticky, score |
| `local` | The worker call itself |
| `overflow` | The burst call, when one happens |
| `e2e` | Everything, end to end |

`METRICS.profile_summary()` prints count, total and mean per stage — the REPL's
`profile` command. `refresh_from_router()` walks both pools and snapshots every
replica into `orch_replica_*` series, which is what turns one gauge per fleet
into one gauge per pod.

---

## The dashboard path

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'fontFamily':'ui-sans-serif, -apple-system, Segoe UI, Inter, Helvetica, Arial, sans-serif','fontSize':'14px',
  'primaryColor':'#ecfdf5','primaryTextColor':'#0f172a','primaryBorderColor':'#059669',
  'secondaryColor':'#f8fafc','tertiaryColor':'#ffffff',
  'lineColor':'#94a3b8','textColor':'#1e293b',
  'clusterBkg':'#ffffff','clusterBorder':'#a7f3d0',
  'edgeLabelBackground':'#ffffff','nodeBorder':'#059669'
}}}%%
flowchart LR
    PY["observability/dashboards.py<br/>panels as Python"]
    JSON["ten .json files<br/>k8s-config/observability/dashboards/"]
    CM["ConfigMap class10-dashboards<br/>label grafana_dashboard=1"]
    SIDECAR["Grafana sidecar"]

    PY -->|"python -m observability.dashboards"| JSON
    JSON -->|"kubectl create configmap"| CM
    CM -->|"watches the label"| SIDECAR
```

Dashboards are **generated, not hand-drawn**. `dashboards.py` holds a
`METRIC_NAMES` map and builds panels from it, so a renamed metric is a one-line
change rather than ten JSON edits. `day2_observability.sh` regenerates them on
every run.

---

---

## One measured request

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'fontFamily':'ui-sans-serif, -apple-system, Segoe UI, Inter, Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'actorBkg':'#ecfdf5','actorBorder':'#059669','actorTextColor':'#0f172a','actorLineColor':'#d1fae5',
  'signalColor':'#64748b','signalTextColor':'#1e293b',
  'noteBkgColor':'#f0fdf4','noteBorderColor':'#16a34a','noteTextColor':'#0f172a',
  'activationBkgColor':'#d1fae5','activationBorderColor':'#047857',
  'labelBoxBkgColor':'#ffffff','labelBoxBorderColor':'#a7f3d0','labelTextColor':'#0f172a',
  'sequenceNumberColor':'#ffffff','loopTextColor':'#1e293b'
}}}%%
sequenceDiagram
    participant C as Client
    participant S as serve.prepare_chat
    participant O as Overflow
    participant A as Gateway
    participant R as Router
    participant M as METRICS

    C->>S: payload
    S->>S: guardrails.inspect, clamp max_tokens
    S->>O: send(req)
    O->>A: handle(req)
    A->>M: observe_duration("gateway")
    A->>R: place(req)
    R->>M: observe_duration("pick")
    R-->>A: Handoff, then worker call
    A-->>O: Response
    O->>M: observe_duration("local")
    O->>M: observe_duration("e2e")
    O-->>C: served, and counted
```

Where each stage is actually recorded:

| Stage | Call site |
|---|---|
| `gateway` | `gateway/admission.py` — five sites, one per exit path |
| `pick` | `router/router.py` — four sites, including the sticky shortcut |
| `local`, `e2e` | `router/overflow.py:131` — in `_finish`, so every outcome is timed |
| `overflow` | `router/overflow.py:134` — only when a burst actually happened |

Every stage boundary writes a histogram sample. That is the whole difference
between module 9 and 10: module 9 tells you *what* happened, 10 tells you *where
the time went*.

---
