# app/ — the Dodona voice agent

Talk to the research agent out loud and watch what it does to the cluster.
Everything here runs on the laptop; the one thing that leaves it is the
completion, and that goes to the gateway on `127.0.0.1:8080` — through guard,
admit, place and queue. A completion that skips the serve path tells us nothing
about the infrastructure this project exists to measure.

```
browser (orb + transcript)
   │  WebRTC
LiveKit server            docker, 127.0.0.1:7880
   │
voice worker              agentship-voice, LiveKit AgentSession
   ├── Deepgram           STT nova-3, TTS aura-2
   └── LLM ─────────────► 127.0.0.1:8080/v1  →  tunnel  →  H100
```

| | |
|---|---|
| `dodona.yaml` | the agent — model, prompt, tools, `api_base` |
| `voice.py` | the LiveKit worker: AgentShip in the `llm=` slot |
| `docker-compose.yml` | LiveKit, in dev mode, bound to localhost |
| `web/` | Next.js UI — animated orb, live transcript, token route |

## Run it

```bash
make ssh        # the tunnel, so :8080 reaches the GPU
make app      # LiveKit + worker + UI, then open http://localhost:3000
```

## What it needs

`DEEPGRAM_API_KEY` in `.env`. One account covers both ears and mouth.

LiveKit runs in `--dev` mode, which uses the well-known `devkey` / `secret`
pair. That is fine for something bound to localhost and is **not** a credential
to reuse anywhere.

## Why these choices

**Deepgram for both.** `nova-3` is purpose-built for streaming speech and is
markedly more accurate on live audio than a batch transcriber fed one VAD
segment at a time. A misheard question is answered confidently and wrongly,
which is worse than a slow answer.

**Silero for voice activity.** Local, free, no key, and it decides the one thing
that cannot be outsourced without adding a round trip: when you stopped talking.

**AgentShip in the `llm=` slot only.** LiveKit has no pipeline node to drop an
agent into — it has an `AgentSession` with `stt` / `vad` / `llm` / `tts` slots.
So the other three are LiveKit's own plugins and the agent is the LLM. Memory,
tracing and tools are inherited from the agent rather than reimplemented for
audio.

## Where the time goes

The SLO chain in `benchmarking/slos.py` budgets a listener's 1.5 s like this:

| stage | budget |
|---|---|
| ASR endpoint and finalize | 300 ms |
| **LLM TTFT** | **800 ms** |
| TTS first chunk | 200 ms |
| network and orchestration | 200 ms |

The engine's 800 ms is the part this repo measures. Speaking to it is how you
find out whether that budget means anything to a person waiting in silence.
