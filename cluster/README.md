# cluster/ — how the engine comes up

Built stage by stage. **[`../RUNBOOK.md`](../RUNBOOK.md) is the thing you execute** —
ten stages, each with a gate you check before starting the next.

| | |
|---|---|
| [`../COMMANDS.md`](../COMMANDS.md) | **Every command, in order.** Marked `[Mac]` or `[GPU]` |
| [`../RUNBOOK.md`](../RUNBOOK.md) | The bring-up, stage 0 → 10, with the gate per stage |
| `k8s/` | Manifests: three HAMi slices, 1 prefill + 2 decode, Mooncake, observability |
| `setup/` | Lambda bring-up, the SSH tunnel, smoke tests |

**Status: not written.** Stage 0 needs no manifests at all — docker and one
container — which is deliberate, so the capacity arithmetic is verified before
any Kubernetes exists.

## The target

```
1 x A100-40GB  ·  ~37 GiB allocatable across pods

slice  pod         gpumem      gpucores
  0    prefill-0   12288 MiB      40%
  1    decode-0    12288 MiB      28%
  2    decode-1    12288 MiB      28%
                   ---------      ----
                   36 GiB          96%
```

```
--model Qwen/Qwen2.5-7B-Instruct-AWQ
--quantization awq_marlin
--max-model-len 16384
--max-num-seqs 64
--tensor-parallel-size 1
--gpu-memory-utilization 0.90
--enable-prefix-caching
--enable-auto-tool-choice --tool-call-parser hermes
```

## Two things about HAMi worth knowing before you read a number

**`gpumem` is what the pod sees as its entire device.** vLLM's
`gpu_memory_utilization 0.90` then applies to *that*, not to the card. A 12 GiB
slice yields a 10.80 GiB budget. Reading the 0.90 against the 40 GiB card
inflates KV by 3×.

**`gpucores` caps a pod's share of SM *time*; it does not give it dedicated
SMs.** So disaggregation's usual argument — that a long prefill cannot stall
another worker's decode loop — only partly holds, because all three pods contend
for the same SMs through a scheduler quota. On three separate cards the isolation
would be physical. Any inter-token spike on decode during a large prefill is this
effect; it is prediction P7.

## The access path

Lambda allows only SSH, so everything goes through one tunnel. Open only
`127.0.0.1` URLs, never the public IP.

| URL | What | After stage |
|---|---|---|
| `http://127.0.0.1:8000` | a vLLM worker directly | 0 |
| `http://127.0.0.1:3000` | Open WebUI | 1 |
| `http://127.0.0.1:8080` | the gateway | 4 |
| `http://127.0.0.1:3001` | Grafana | 3 |

## Reference, not inheritance

The course lab (gateway, router, Mooncake, HAMi, ten dashboards) is where this
cluster's shape comes from, and we port from it piece by piece rather than
running it as a black box. Component-level docs for that stack:
[`../docs/cluster/`](../docs/cluster/README.md).
