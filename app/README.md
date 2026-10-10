# app/ — the Dodona voice agent

Talks **only** to our serve path. Completions that skip guard / admit / place /
queue do not count, so there is no direct route from here to a worker.

| | |
|---|---|
| `dodona.yaml` | the agent spec — model, prompt, tools, `api_base` |
| `questions.jsonl` | the fixed question set, so runs are comparable |
| `run.py` | drives N concurrent sessions, writes per-question traces |
| `voice.py` | the Deepgram loop — STT in, TTS out |

**Status: not written.** Stages 1 and 9 of
[`../RUNBOOK.md`](../docs/RUNBOOK.md).

## Shape

```yaml
model: openai/Qwen2.5-7B-Instruct-AWQ
params:
  api_base: "http://127.0.0.1:8080/v1"
  temperature: 0.3
  top_p: 0.8
  max_tokens: 1024
tools: [web_search, scrape_url, http_request, calculator]
```

`openai/<model>` because LiteLLM's `openai` provider speaks to any
OpenAI-compatible endpoint, and our gateway is one.

## Two settings that are choices, not defaults

Both change the traffic shape, so a reader would otherwise mis-attribute what
they cause.

- **`scrape_url` truncated to 12k tokens**, so a turn-4 request fits inside
  `max_model_len` 16,384. Without it a long article plus three turns of history
  overruns the context and vLLM refuses — and an engine context refusal looks
  nothing like an admission refusal in the decision log.
- **One tool call per reply.** The `hermes` parser returns the *entire batch* as
  text when any single call in it is malformed, losing the valid calls with it.

## Voice

Deepgram for both directions, off-GPU. The agent speaks an acknowledgement
immediately (**time-to-first-audio < 1 s**) and narrates the researched answer
when it lands (**10–30 s**). Those are two different SLOs on one request; see
[`../PROJECT.md`](../docs/PROJECT.md#two-slos-on-one-request).

A listener talking over the answer **aborts** mid-generation, which makes the
`aborted` KV-release path common rather than exotic. Part 5 covers who frees it.

## Prove it on a hosted model first

```
python run.py --questions questions.jsonl --concurrency 1 --model openai/gpt-4o-mini
```

Deliberate: a broken agent and a broken gateway produce the same symptom, and
separating them after the box is rented costs an hour.
