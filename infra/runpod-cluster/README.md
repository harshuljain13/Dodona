# runpod-cluster/ — the same fleet, without an orchestrator

One vLLM per GPU, the gateway in front, started by a shell script instead of
Kubernetes. For boxes that are **containers** — which is most rented GPUs, and
where [`../lambda-cluster/`](../lambda-cluster/README.md) cannot run at all:
k3s installs itself as a systemd service, and a container has no systemd.

```bash
make fleet-runpod              # sync, install, start, and open the tunnel
make fleet-runpod-status       # model, KV pressure, GPU placement, uptime
make fleet-runpod-smoke-test   # does a request come back?
make fleet-runpod-logs         # follow it (it runs detached)
make fleet-runpod-down         # stop it
```

| | |
|---|---|
| `setup/` | `workers.sh` starts the fleet · `down.sh` stops it · `status.sh` · `smoke.sh` · `observability.sh` installs prometheus and grafana |
| `observability/` | `gpu_exporter.py` · `prometheus.yml.tmpl` · `grafana/` provisioning |
| [`../setup/`](../setup/) | shared with lambda-cluster: `up.sh`, `sync.sh`, `engine.sh`, `box_packages.sh` |
| [`../control/`](../control/) | the gateway, router and guardrails — **identical on both paths** |

## The layout

```
3 x A100-80GB  ·  one worker per card, nothing to slice

GPU 0   prefill    :8100     builds the prompt's KV
GPU 1   decode-0   :8101     generates
GPU 2   decode-1   :8102     generates

        gateway    :8080     guard · admit · place · queue · hop
        mooncake   :50051    the hop ledger
        gpu export :9400     DCGM_FI_DEV_* from nvidia-smi
        prometheus :9090
        grafana    :31495    admin / $GRAFANA_PASSWORD
```

Ports come from `PORT_BASE` (default 8100, not 8000: RunPod's Jupyter template
runs nginx on :8001). `make ssh` and `workers.sh` read the same value, so the
tunnel follows the fleet.

## What this path does not have

| | Why |
|---|---|
| **HAMi** | It slices one card into three. With one GPU per worker there is nothing to slice |
| **KEDA** | Needs Kubernetes. And scaling takes 2–5 minutes against a signal that moves in seconds — which is the argument *for* admission control, not a gap in it |
| **k3s** | Installs as a systemd service; a container has no init to hand it to |
| `make kv`, `make logs` | Both shell out to `k3s kubectl`. Use `make fleet-runpod-logs` |
| 4 of the 10 dashboards | `cluster`, `replicas`, `hami`, `keda` query `kube_*` and `container_*` — objects that do not exist here. They are **not provisioned**, so you never open an empty one |

## The number that decides whether a run means anything

`SLICE_MIB` caps each worker's memory. `0` means take the whole card, which is
right when each worker has its own GPU.

Too high and the KV pool never fills: 0.90 of an 80 GiB card holding a 7B gives
a ~66 GiB cache, `kv_cache_usage_perc` never reaches the 0.90 the policy is
thresholded at, and the run comes back **INERT** — numbers indistinguishable
from having no admission policy at all. Too low and it is smaller than the
weights, and vLLM will not start.

With a 32B on 80 GiB: ~19 GiB of weights, ~52 GiB of KV, and a 12k-token
request costs 3 GiB — so seventeen concurrent sessions fill it, which a load
test reaches. That is why the model is 32B here and not 7B. See
[`docs/project/01-capacity.md`](../../docs/project/01-capacity.md).
