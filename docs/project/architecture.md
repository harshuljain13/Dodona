# Architecture — the C4 levels

The system from outside, then the pieces it is made of, then inside the piece we
write. The single high-level view lives in
[`../../README.md`](../../README.md#architecture); these are the levels beneath it.
The [C4 model](https://c4model.com): the system from outside, then the pieces,
then inside the piece we write. The single high-level view is in
[`../../SUBMISSION.md`](../../SUBMISSION.md#high-level); these are the levels beneath it.

### Level 1 — Context

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'Inter, Segoe UI, Helvetica Neue, Arial, sans-serif','fontSize':'15px','lineColor':'#000000','clusterBkg':'#ffffff','clusterBorder':'#94a3b8'}}}%%
flowchart LR
  USER(["Listener<br/>[Person]<br/>asks aloud"])
  OPERATOR(["Us<br/>[Person]<br/>run load, read report"])

  SYS["<b>Dodona</b><br/>[Software System]<br/>Answers spoken questions<br/>with citations.<br/>Decides what to admit.<br/>Records why"]

  DG[["Deepgram<br/>[External]<br/>speech ↔ text"]]
  WEB[["Web<br/>[External]<br/>search and fetch"]]
  HF[["Hugging Face<br/>[External]<br/>model weights"]]
  OVF[["Superlinked<br/>[External]<br/>Qwen3.5-4B · overflow only<br/>capped at 20 requests"]]

  USER -->|"speaks a question"| SYS
  SYS -->|"speaks the answer"| USER
  OPERATOR -->|"reads the report"| SYS
  SYS <-->|"transcribe · synthesise"| DG
  SYS -->|"fetches sources"| WEB
  SYS -->|"downloads weights"| HF
  SYS -.->|"only when genuinely full"| OVF

  class USER,OPERATOR input
  class SYS primary
  class DG,WEB,HF,OVF accent
  classDef primary fill:#dbeafe,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef accent fill:#f3e8ff,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef input fill:#ffe4e6,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  linkStyle default stroke:#000000,stroke-width:2px
```

The dotted line is the only exit from the cluster and it is for capacity failures
alone. A tenant refusal stays local — a caller over their own quota being served
on capacity we pay for is overspending, not a capacity problem.

Deepgram is solid-line and external on purpose: speech is in the request path but
**off the GPU**, so it is a network constant rather than something competing for
SM time.

### Level 2 — Containers

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'Inter, Segoe UI, Helvetica Neue, Arial, sans-serif','fontSize':'14px','lineColor':'#000000','clusterBkg':'#ffffff','clusterBorder':'#94a3b8'}}}%%
flowchart TB
  subgraph CLIENTS["Clients"]
    AG["<b>Dodona agent</b><br/>[Container]<br/>search · fetch · read · cite"]
    UI["<b>Open WebUI</b><br/>[Container]<br/>hand testing, stage 1"]
    LOAD["<b>Load generator</b><br/>[Container]<br/>same trace every run"]
  end

  subgraph OURS["control/ — what we write"]
    GW["<b>Gateway</b><br/>[Container :8080]<br/>guard · admit · place<br/>queue · hop · warm<br/>writes the decision log"]
    AP["<b>AdmitPerf</b><br/>[Library]<br/>policy registry<br/>report generator"]
  end

  subgraph ENGINE["What we configure but do not touch"]
    PF["<b>vLLM prefill</b><br/>[Container ×1]<br/>slice 0 · 12 GiB"]
    DC["<b>vLLM decode</b><br/>[Container ×2]<br/>slices 1-2 · 12 GiB"]
    MC[("<b>Mooncake</b><br/>KV store")]
    OBS[("<b>Prometheus<br/>+ Grafana</b>")]
  end

  AG --> GW
  UI --> GW
  LOAD --> GW
  GW <-->|"decide, then report"| AP
  GW --> PF
  PF --> MC --> DC
  GW --> OBS
  PF --> OBS
  DC --> OBS

  class AG,UI,LOAD accent
  class GW primary
  class AP highlight
  class PF,DC secondary
  class MC data
  class OBS neutral
  classDef primary fill:#dbeafe,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef secondary fill:#dcfce7,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef accent fill:#f3e8ff,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef data fill:#ffedd5,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef neutral fill:#f3f4f6,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef highlight fill:#e8e0ff,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  linkStyle default stroke:#000000,stroke-width:2px
```

**Open WebUI comes before the agent** — stage 1 points it straight at the engine
for hand testing, so a broken agent and a broken gateway are never confused for
each other. The load generator exists for the one thing it is good at: replaying
the *same* trace so two runs can be compared.

### Level 3 — Inside the gateway

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'Inter, Segoe UI, Helvetica Neue, Arial, sans-serif','fontSize':'14px','lineColor':'#000000','clusterBkg':'#ffffff','clusterBorder':'#94a3b8'}}}%%
flowchart LR
  subgraph GW["control/"]
    G[/"<b>guard</b><br/>inspect(payload)<br/>reject malformed"/]
    T{"<b>tenant window</b><br/>over share? → 429"}
    A{"<b>admit</b><br/>should_shed()<br/>a socket, not a rule"}
    P["<b>place</b><br/>pick(req, workers)"]
    H["<b>queue + hop</b><br/>transfer(src, dst)<br/>declare warm"]
    L[("<b>decision log</b><br/>one row each:<br/>signal value, verdict")]
    G --> T --> A --> P --> H
  end

  POL[["<b>Policy</b><br/>[AdmitPerf]<br/>declares its 5 axes<br/>reads its signal<br/>returns a verdict"]]
  REP(["<b>Report</b><br/>policy card · range<br/>liveness verdict"])

  A -->|"decide(request, state)"| POL
  A -->|"write row"| L
  L --> REP

  class G,P,H primary
  class T,A decision
  class POL highlight
  class L data
  class REP success
  classDef primary fill:#dbeafe,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef decision fill:#fef3c7,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef data fill:#ffedd5,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef success fill:#ccfbf1,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  classDef highlight fill:#e8e0ff,stroke:#000000,stroke-width:2px,color:#0f172a,font-weight:bold
  linkStyle default stroke:#000000,stroke-width:2px
```

Admission reads what the workers are doing, hands that plus the request to a
policy from the registry, does what comes back, and writes one row. The policy
declares its five axes at registration — unit, setting, objective, signal,
portability — so the report card fills itself in.

**The tenant window sits before admission**, and the ordering is load-bearing:
a quota refusal is client-attributable, so it is a 429, and a 429 must never
reach the paid overflow provider.

---
