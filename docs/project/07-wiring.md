# Part 7 — Wire the app to the cluster

Every completion the agent makes goes through the gateway. Nothing talks to a
vLLM worker directly.

```
AgentShip  --openai/<model> + api_base-->  gateway :8080  -->  vLLM pools
```

## The blocker, confirmed

`cluster/router/pools.py:193` builds the downstream payload as:

```python
payload = {
    "model": self.model,
    "messages": request_messages(req),
    "max_tokens": max(1, int(req.max_new_tokens)),
}
```

Three keys. **`tools`, `tool_choice`, `temperature`, `top_p`, `stop` and
`stream` are all dropped**, silently, after surviving the guardrails.

For a chat app that is invisible. For a research agent it is fatal: with no tool
schemas the model has nothing to call, so it answers the question from its own
weights in prose, produces no `tool_calls`, and the agent loop terminates after
one turn. The symptom looks like a bad prompt or a weak model. It is neither.

This also silently removes the workload's defining property. Without tool calls
there are no multi-turn sessions, no growing context, and no 12k prefills — the
four reasons [Part 0](00-use-case.md) gives for choosing this app. The run would
measure a single-turn chat workload and the report would not say so.

### The fix

A pass-through allowlist rather than forwarding the payload wholesale, because
the gateway's own control keys (`kv_hop`, `prefix_hash`, `tenant`, `priority`,
`arrival_t`) must not reach vLLM, and an unknown key is a 400 from the engine.

| Forwarded | Kept at the gateway |
|---|---|
| `tools`, `tool_choice` | `kv_hop`, `kv_evict`, `prefix_hash` |
| `temperature`, `top_p`, `top_k` | `tenant`, `priority`, `arrival_t`, `timeout_s` |
| `stop`, `presence_penalty`, `repetition_penalty` | `capability`, `id` |
| `response_format`, `seed` | |

`messages`, `model` and `max_tokens` keep their current handling — the gateway
owns the token limit because `max_tokens` is a capacity decision.

`stream` stays special. The gateway already synthesises SSE from a completed
response (`gateway/serve.py:141`), so streaming works at the edge without the
worker streaming. That is fine for the agent, which waits for a whole message
anyway. **It makes gateway-measured TTFT meaningless**, so TTFT comes from vLLM's
own metrics, never from the client. Worth stating because a TTFT number from the
wrong side of that boundary is the easiest wrong number in the whole report.

Needs a test: a payload carrying `tools` arrives at a fake worker with `tools`
intact, and one carrying `kv_hop` arrives without it.

## AgentShip side

`api_base` is already plumbed: `agentship_langgraph/models.py:34` maps it
through to the model, and `examples/local.yaml` is the existing proof —
`ollama/llama3` with `params.api_base`.

So the agent spec is a provider swap:

```yaml
name: deep-research
engine: langgraph
model: openai/Qwen2.5-32B-Instruct-AWQ
prompt: |
  You are a research assistant. Plan, then use the tools to find and read
  sources. Cite every claim with the URL you read it from.
tools: [web_search, scrape_url, http_request, calculator]
params:
  api_base: "http://127.0.0.1:8080/v1"
  temperature: 0.3
  top_p: 0.8
  max_tokens: 1024
```

`openai/<model>` rather than a custom provider because LiteLLM's `openai`
provider speaks to any OpenAI-compatible endpoint and the gateway is one.
`agentship-langgraph` declares `openai` in its `providers` set, so the build
gate passes.

### Settings that are choices, not defaults

| Setting | Value | Why |
|---|---|---|
| `temperature` | 0.3 | Greedy decoding on Qwen2.5 degenerates into `<\|im_start\|>` repetition and emits tool calls as prose. Qwen's own recommended sampling avoids it |
| `top_p` | 0.8 | Same reason |
| `max_tokens` | 1024 | Per call. Enough for a tool call plus reasoning; small enough that a runaway generation does not hold a decode slot for minutes |
| `scrape_url` truncation | 12k tokens | So a turn-4 request fits in `max_model_len`. See [Part 0](00-use-case.md#why-12k-and-not-15k) |
| one tool call per reply | yes | A batch where one call is malformed gets rejected wholesale by the parser, losing the valid calls with it |

The last two are the ones a reader would otherwise assume are defaults. They are
not; they were chosen, and they change the traffic shape.

## Tool calling on the engine side

vLLM needs to be told to parse tool calls, or they arrive as text in `content`
and AgentShip sees no `tool_calls` array:

```
--enable-auto-tool-choice --tool-call-parser hermes
```

`hermes` is the parser for Qwen2.5. One known sharp edge: when the model emits
several calls in one reply and any one of them is malformed, the parser returns
the **entire batch** as text rather than the valid subset. That is the reason for
one tool call per reply above.

## The access path

Most GPU hosts allow only SSH inbound, so everything goes through one tunnel
(`infra/lambda-cluster/setup/ssh.sh`). Open only `127.0.0.1` URLs, never the public IP.

| URL | What |
|---|---|
| `http://127.0.0.1:8080` | the gateway — AgentShip's `api_base`, and Locust's `--host` |
| `http://127.0.0.1:30030` | Open WebUI, for the live demo |
| `http://127.0.0.1:31495` | Grafana |

The agent runs on the laptop, not in the cluster. That adds laptop-to-box
round-trip time to every call, which inflates wall-clock per question and does
**not** affect TTFT or KV pressure. Said here so the wall-clock numbers in the
report are read correctly.

## Verification, in order

Each step fails for a different reason, so running them in order localises a
break instead of leaving the whole path suspect.

| # | Check | Passes when |
|---|---|---|
| 1 | `curl` the gateway with a plain completion | 200, text back |
| 2 | `curl` the gateway with a `tools` array | response contains `tool_calls`, not prose |
| 3 | AgentShip one question, one turn | `web_search` fires |
| 4 | AgentShip full question | multi-turn, cites a URL it actually fetched |
| 5 | Decision log non-empty, `admitperf_gate_recording 1` | one row per completion |
| 6 | Grafana shows the traffic on the gateway and vLLM panels | both, not one |

Step 2 is the one that fails today. Step 5 is the one that is easy to skip and
produces an empty report an hour later.
