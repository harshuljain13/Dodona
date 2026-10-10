# Cluster documentation — how the serving stack works

**These docs describe the code in [`../`](../README.md), file by file.** They are
inherited from the upstream lab and carried into module 11 with the stack, so they stay
accurate as the code does.

For *this project's* decisions — why 4 × A100, why int4, why disaggregated, what
is being measured — go up to [`../../docs/`](../README.md). Start from
[`../../SUBMISSION.md`](../SUBMISSION.md) if you have not.

| Page | Read it for |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The overview and component map |
| [DATAFLOW.md](DATAFLOW.md) | One request through the code, file by file |
| [gateway.md](gateway.md) | Entry, guardrails, admission |
| [router.md](router.md) | Placement, scoring, shedding |
| [kvbus.md](kvbus.md) | Prefix bookkeeping and the KV hop |
| [observability.md](observability.md) | Metrics, profiling, dashboards |
| [kernels.md](kernels.md) | Attention math, KV eviction, warmup, transports |
| [infrastructure.md](infrastructure.md) | Access, startup, HAMi, KEDA |
| [COMMANDS.md](COMMANDS.md) | Debugging commands |

The lab steps themselves are in the [stack README](../README.md).

**New here?** [DATAFLOW.md](DATAFLOW.md) answers "where does a request go" better
than the architecture overview does.

## Where these docs are superseded by module 11

Two places where module 11's configuration contradicts what is written here.
The deep dive is still the right description of the *code*; the *deployment* has
moved.

| Page | What changed | Authority |
|---|---|---|
| [infrastructure.md](infrastructure.md) | **HAMi GPU slicing is gone.** The upstream lab sliced one physical card into two 16 GiB shares for a 3B model. Module 11 runs one worker per physical card — `nvidia.com/gpu: "1"` and nothing else — because a 16 GiB slice cannot hold 19.4 GiB of weights | [`../project/02-cluster.md`](../project/02-cluster.md#what-has-to-change-for-four-physical-gpus) |
| [DATAFLOW.md](DATAFLOW.md) | **The worker payload gains a forward allowlist.** `router/pools.py` forwarded `{model, messages, max_tokens}` only, which drops tool schemas and makes a tool-calling agent impossible | [`../project/07-wiring.md`](../project/07-wiring.md#the-blocker-confirmed) |

Everything else — the gateway path, placement scoring, the KV bus, the metrics
and dashboards — is unchanged.
