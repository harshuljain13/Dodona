# app/

The voice agent. **Everything here runs on the laptop. The only thing that
leaves it is the completion**, and that goes to the gateway on
`127.0.0.1:8080` — through guard, admit, place and queue — because a completion
that skips the serve path tells us nothing about the infrastructure this
project exists to measure.

```
browser (:3000)           mic → 24 kHz PCM, one websocket
AgentShip service (:8090) /voice + /v1 + the agent
  voice cascade           deepgram nova-3 · silero VAD · deepgram aura-2
  Dodona agent            LangGraph, web_search + scrape_url
    └─ completions ─────▶ the gateway, :8080
```

| File | What it is |
|---|---|
| `dodona.yaml` | the agent. Engine, prompt, tools, and the `voice:` block that builds the cascade |
| `run.sh` | what `make app` runs: the service, then the UI |
| `web/` | Next.js — the orb, the transcript, and the mic |

## Run it

```bash
make ssh        # the tunnel, so :8080 reaches the GPU
make app        # service + UI, then open http://localhost:3000
```

## What it needs

`DEEPGRAM_API_KEY` for the ears and the mouth, and `FIRECRAWL_API_KEY` for what
the agent reads. Both in `.env`.

The Python deps are in `requirements-app.txt`, not `requirements.txt` — a laptop
that only drives load should not have to build the speech stack. First `make
app` installs them into `.venv`, and `npm install`s the UI, so there is no
separate setup step.

No Docker. Nothing in the app is a container.

## Why these choices

**AgentShip owns the whole cascade.** `framework: pipecat` means the `voice:`
block in `dodona.yaml` is what actually builds the session — greeting,
`tts_speed`, `endpoint_silence_ms`, `allow_interruptions`, `fallback_text` are
read, not decorative.

This used to be LiveKit, and the reason it is not is worth keeping: WebRTC earns
its keep across a network, and this audio never leaves the host. What it cost
was a container, a token route, an ICE path to get wrong, and a half-implemented
adapter — `LiveKitAdapter.run()` raises `NotImplementedError`, so only `host()`
and `witness()` existed and a worker had to hand-build the session beside the
spec. On the pipecat path there is nothing to hand-build.

**One socket, two payload kinds.** Audio is binary 16-bit PCM at 24 kHz; events
are JSON text on the same connection. 24 kHz because that is what the
synthesiser produces and it will not resample — play it back at 16 kHz and the
agent sounds slow and deep, with nothing reporting an error.

**The page is told the state of the cluster before anyone clicks.** If the
gateway is unreachable the UI says so and the agent says so out loud when it
joins, rather than answering with silence.
