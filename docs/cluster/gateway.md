# Gateway — entry, guardrails and admission

Everything that happens to a request before the router sees it.

Part of the [module 10 docs](README.md). Overview: [ARCHITECTURE.md](ARCHITECTURE.md) · Request path: [DATAFLOW.md](DATAFLOW.md)

---

## The guardrail path

New in 10, and it runs *before* admission:

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'fontFamily':'ui-sans-serif, -apple-system, Segoe UI, Inter, Helvetica, Arial, sans-serif','fontSize':'14px',
  'primaryColor':'#eff6ff','primaryTextColor':'#0f172a','primaryBorderColor':'#94a3b8',
  'secondaryColor':'#f1f5f9','tertiaryColor':'#f8fafc',
  'lineColor':'#7c8da6','textColor':'#1e293b',
  'clusterBkg':'#f8fafc','clusterBorder':'#cbd5e1',
  'edgeLabelBackground':'#ffffff','nodeBorder':'#94a3b8'
}}}%%
flowchart LR
    REQ["incoming payload"] --> GUARD["<b>guardrails.inspect</b><br/>app/guardrails.py"]
    GUARD -->|"ok"| ADMIT["Gateway.handle<br/>tenant cap"]
    GUARD -->|"400"| REJECT["model_not_allowed · bad_max_tokens<br/>prompt_too_long · empty_prompt<br/>vision_needs_image"]
```

Five rejections, all **400**, all before any routing happens. It also *mutates*
the payload: a missing `max_tokens` is defaulted to 512, anything above 512 is
clamped down, and `tenant` defaults to `lab`.

This matters because Open WebUI is a real client sending arbitrary requests —
guardrails are what stop a browser from asking for 100 000 tokens.

| Rejection | Cause |
|---|---|
| `model_not_allowed` | Model not in the local vLLM allowlist |
| `bad_max_tokens` | Non-integer or <= 0 |
| `prompt_too_long` | Over 8 000 characters |
| `empty_prompt` | Text model with no text |
| `vision_needs_image` | Vision model with no image part |
