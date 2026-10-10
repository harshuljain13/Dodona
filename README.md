# Dodona

*A voice research agent you can actually host, and the cluster that decides what
it is allowed to ask.*

You speak a question about herbs and supplements. Dodona searches, fetches
sources, reads them, and answers aloud **with citations**. Every token of
inference runs on **one A100-40GB you rent** — no hosted chat API anywhere in the
answer path. Deepgram does speech-to-text and text-to-speech off-GPU; the GPU
serves only the model.

> The oracle at Dodona answered questions by interpreting the sound of wind
> through the oak leaves. Voice in, sourced answer out.

---

## Start here

| | |
|---|---|
| [**`SUBMISSION.md`**](SUBMISSION.md) | The brief answered part by part, with a decision tree per part. **The go-to file** |
| [**`PROJECT.md`**](PROJECT.md) | What the project is, and the C4-style architecture levels |
| [**`docs/`**](docs/README.md) | Deep dives, split into **project-level** and **cluster-level** |

## Why this is a serving project, not an app project

The agent is the easy half. Three things about this workload make the control
plane the interesting part:

**Voice gives every request a real deadline.** One request carries **two
different SLOs**: time-to-first-audio has to be under a second — an
acknowledgement, *"let me look that up"* — while time-to-full-answer is 10–30
seconds of searching, fetching and a 12k-token prefill. Admission has to reason
about both.

**The workload is the hard serving shape.** A fetched source arrives as a single
~12,000-token prefill holding **0.66 GiB of KV — 14% of everything a decode
worker has**. The system prompt plus tool schemas are a ~1k shared prefix,
byte-identical every turn. Context grows within a session, so a request that was
cheap on turn 1 is expensive by turn 4. Load is bursty because the agent goes
quiet during tool calls, not because a generator was told to make it so.

**The GPU is scarce by construction.** One card, three slices, a decode pool of
**11 concurrent sequences** at `max_model_len`. The cluster genuinely has to turn
work away.

## The configuration

```
1 x A100-40GB  ·  Qwen2.5-7B-Instruct-AWQ (int4)  ·  TP=1
3 HAMi slices x 12 GiB  ·  1 prefill + 2 decode  ·  max_model_len 16,384

kv_bytes_per_token = 2 x 28 layers x 4 kv_heads x 128 head_dim x 2 B = 56 KiB
per slice          = 12 x 0.90 = 10.80 GiB budget
                     - 5.2 weights - 0.8 runtime = 4.80 GiB KV
one 16k sequence   = 0.875 GiB  ->  5.5 seq/replica  ->  decode pool 11.0
max_num_seqs = 64  ->  KV binds
```

Voice (Deepgram STT/TTS) is off-GPU and deliberately so — it is a network
constant, which keeps the contention we measure purely LLM-side.

## Layout

Three documents at the top, then one directory per thing that actually runs.

```
dodona/
  SUBMISSION.md      the brief answered, parts 0-8
  PROJECT.md         the project and the C4 levels
  app/               the voice agent — talks only to our serve path
  control/           guard · admit · place · queue · hop · warm · stay-or-leave
  cluster/           how the engine comes up, stage by stage
  docs/
    project/         per-part deep dives + the results report
    cluster/         the stack, file by file
  notebook/          part5-queue.ipynb — the brief submits this separately
  metrics/           scrapes, hop dicts, evict counts, 429/503
  assets/screenshots/  Grafana exhibits and generated charts
```

## Status

Built **incrementally** — each stage ends with something testable before the next
begins. Runbook: [`cluster/RUNBOOK.md`](cluster/RUNBOOK.md).

| # | Stage | State |
|---|---|---|
| 0 | vLLM on the bare box, metrics visible, `kv_bytes_per_token` verified | not started |
| 1 | Open WebUI for hand testing | not started |
| 2 | k3s + HAMi, 3 slices, 1 prefill + 2 decode | not started |
| 3 | Prometheus + Grafana — **the brief's pass/fail scrape** | not started |
| 4 | Gateway on :8080, pass-through only | not started |
| 5 | Guardrails + tenant window | not started |
| 6 | Admission as a plug-in socket, with a decision log | not started |
| 7 | Placement across both decode pods, queue depth as a scorer | not started |
| 8 | KV hop + warm gate — **the other half of the pass/fail** | not started |
| 9 | The voice agent through the gateway | not started |
| 10 | Measure: load levels, baselines, report | not started |

**Every number in these docs is predicted.** Nothing has run on hardware yet.
Predictions are written down before the run and checked afterwards without being
edited — a prediction that was wrong by 2× tells you which piece of arithmetic
was misunderstood.

## Prior art this builds on

| | |
|---|---|
| [AdmitPerf](https://github.com/harshuljain13/AdmitPerf) | The policy registry, the per-decision log, the report generator |
| [AgentShip](https://github.com/Agent-Ship/agent-ship) | The agent framework, and `agentship-voice` for the speech loop |
| [llm-inference-experiments](https://github.com/harshuljain13/llm-inference-experiments) | Modules 1–10. The class-10 lab is where this cluster's shape comes from |
