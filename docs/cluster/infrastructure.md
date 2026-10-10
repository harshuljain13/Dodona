# Infrastructure — access, startup, HAMi and KEDA

How you reach the cluster, why pods look ready before they are, and what the two controllers do.

Part of the [module 10 docs](README.md). Overview: [ARCHITECTURE.md](ARCHITECTURE.md) · Request path: [DATAFLOW.md](DATAFLOW.md)

---

## Access path — the SSH tunnel

Lambda's firewall allows only SSH, so nothing is reachable directly. One
`ssh -L` session carries everything:

```mermaid
flowchart LR
    MAC["Mac<br/>127.0.0.1"]
    SSH["ssh -L x7<br/>setup/ssh.sh"]
    BOX["Lambda GPU box"]

    MAC --> SSH
    SSH --> BOX
```

| Local port | Reaches |
|---|---|
| 8000, 8001 | vLLM text and vision engines |
| 8080 | orch-serve gateway |
| 30080 | orch-serve NodePort |
| 50051 | Mooncake store |
| 30030 | Open WebUI |
| 31495 | Grafana |

## Reaching anything — the full hop chain

Every URL you open traverses five layers. Knowing the order is how you tell a
dead tunnel from a dead pod.

```mermaid
sequenceDiagram
    participant B as Browser (Mac)
    participant S as ssh -L (Step 2)
    participant N as Node loopback
    participant K as kube-proxy / hostPort
    participant P as Pod

    B->>S: GET 127.0.0.1:31495
    S->>N: forward to box 127.0.0.1:31495
    alt nothing bound on the box
        N-->>S: connection refused
        S-->>B: channel N: open failed / ERR_CONNECTION_RESET
    else bound
        N->>K: NodePort or hostPort rule
        K->>P: container port
        P-->>B: response
    end
```

| Symptom | Layer that failed |
|---|---|
| `channel N: open failed` | Nothing listening on the box for that port |
| `ERR_CONNECTION_RESET` | Same — SSH accepts locally, then resets when the remote refuses |
| Shell works, one URL dead | That service only; the tunnel is fine |
| All URLs dead, shell alive | The forwards died — restart `setup/ssh.sh` |
| Works from the box, not the Mac | The forward, not the service |

**Ports are bound three different ways here**, which is why they fail
differently. vLLM and orch-serve use `hostPort` on the pod, so they bind as soon
as the *container* starts. Open WebUI, mooncake and the vLLM Services also have
pinned `nodePort`s. **Grafana's `nodePort` is not pinned** — `grafana-values.yaml`
sets `type: NodePort` with no port, so Kubernetes allocates randomly and it will
usually *not* match the `31495` that `ssh.sh` forwards. `kubectl port-forward`
sidesteps all of it by binding the box's loopback directly.

## Pod startup — why `READY 1/1` lies

There are **no readiness probes** on the vLLM pods. `1/1` flips when the
container process starts, which is minutes before vLLM serves anything.

```mermaid
sequenceDiagram
    participant K as kubelet
    participant C as container
    participant V as vLLM engine core
    participant H as hostPort 8000/8001

    K->>C: start
    C-->>K: running
    Note over K: READY 1/1 — from here the pod LOOKS up
    C->>V: init engine
    V->>V: download weights
    V->>V: load safetensors (~7.2 GiB for the VL model)
    V->>V: torch.compile, inductor, Dynamo
    V->>V: capture 67 CUDA graph sizes
    Note over V: several minutes, almost no log output
    V->>H: bind and serve
    Note over H: only NOW does curl succeed
```

Everything between `READY 1/1` and `bind` is dead time in which `smoke_sliced.sh`
fails and looks like a crash. Wait for the port, not the pod:

```
until curl -sf http://127.0.0.1:8001/v1/models >/dev/null 2>&1; do sleep 10; done
```

If the engine dies in that window, the API server prints an asyncio traceback
ending in "see root cause above" — the real error is on the `(EngineCore_DP0 …)`
lines before it. `kubectl logs … | grep -v '(APIServer'` is how you see it.

## Dashboards — generated, not written

```mermaid
sequenceDiagram
    participant D as day2_observability.sh
    participant H as Helm
    participant PY as observability/dashboards.py
    participant CM as ConfigMap
    participant SC as Grafana sidecar
    participant G as Grafana

    D->>H: install prometheus, then grafana
    D->>PY: python -m observability.dashboards
    PY-->>D: ten .json files
    D->>CM: create configmap class10-dashboards
    D->>CM: label grafana_dashboard=1
    SC->>CM: watches that label
    SC->>G: imports the ten dashboards
```

The label is load-bearing: without `grafana_dashboard=1` the sidecar ignores the
ConfigMap and Grafana comes up empty. And because the JSON is **regenerated on
every run**, hand-edits to the files are overwritten — change `METRIC_NAMES` in
`dashboards.py` instead.

## The autoscale loop

```mermaid
sequenceDiagram
    participant O as orch-serve
    participant P as Prometheus
    participant K as KEDA
    participant D as Deployment
    participant PL as planner.plan

    loop every scrape interval
        P->>O: GET /metrics
        O-->>P: orch_tokens_in_flight{phase}
    end
    loop every polling interval
        K->>P: trigger query
        P-->>K: value vs threshold 1
        K->>D: set replicas (min..max)
    end
    Note over PL: runs in-process, prints only
    PL->>O: orch_planner_desired_replicas
    Note over PL,K: published, read by no ScaledObject
```

Two independent loops on different intervals — which is why a burst shows in
Grafana well before replicas move. The planner is beside both, not inside
either.

**This is the single point of failure for the whole lab.** Every `127.0.0.1` URL
dies with that session, so a dashboard that stops loading usually means the
Step 2 terminal closed, not that the cluster broke. `ServerAliveInterval=30`
keeps it from idling out.
