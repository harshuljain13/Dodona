# metrics/

Raw scrapes, so a number in a doc can be traced to the scrape it came from.

```
metrics/<run>/
  meta.json          config, commit sha, model, vLLM version, seed
  decisions.jsonl    one row per admission decision, with the signal value
  report.txt         admitperf report decisions.jsonl
  pods/              /metrics per pod, before and after
  hops.json          Mooncake /hops — count, bytes, failed writes, evictions
  shed.json          429 / 503 / 529 counts by reason
```

## The runs

| Run | Policy | Concurrent sessions |
|---|---|---|
| `low-baseline-{a,b,c}` | `no_admission` | 2 |
| `low-{a,b,c}` | `kv_threshold` 0.90 | 2 |
| `high-baseline-{a,b,c}` | `no_admission` | 14 |
| `high-{a,b,c}` | `kv_threshold` 0.90 | 14 |

Three seeds each, because a single run cannot distinguish a threshold crossing
from noise.

## Read a bundle in this order

1. **`meta.json`** — is this the fleet you think it is? Three pods, right model,
   right commit.
2. **The liveness verdict** on line one of `report.txt`. If it says `FAILED` the
   signal never reached the threshold, and nothing downstream of that is about
   the policy.
3. **The signal range** — min / p50 / p95 / p99 / max.
4. Only then the latency and throughput tables.

Going in the other order is how an inert run gets read as a working policy.

## What an empty bundle means

Three different things, not interchangeable:

| | |
|---|---|
| No `decisions.jsonl` | the gate never ran. Check `admitperf_gate_active` |
| Empty file, gate active | the log was unwritable. Check `admitperf_gate_recording` |
| Rows present, last few missing | the run ended without a flush — and the lost rows are the saturation event |

None of these is "the policy never fired". That is a fourth thing, and only the
report can tell you it happened.

**Status: no bundles yet.**
