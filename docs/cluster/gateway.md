# Gateway — entry, guardrails and admission

Everything that happens to a request before the router sees it.

Part of the [module 10 docs](README.md). Overview: [ARCHITECTURE.md](ARCHITECTURE.md) · Request path: [DATAFLOW.md](DATAFLOW.md)

---

## The guardrail path

New in 10, and it runs *before* admission:

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'darkMode':true,'background':'#0B0B0B',
  'fontFamily':'Helvetica, Arial, sans-serif','fontSize':'13.5px',
  'primaryColor':'#111827','primaryTextColor':'#E5E7EB','primaryBorderColor':'#475569',
  'lineColor':'#7C8CA6','textColor':'#E5E7EB','titleColor':'#E5E7EB',
  'clusterBkg':'#0F1218','clusterBorder':'#263142','edgeLabelBackground':'#0B0B0B'
}}}%%
flowchart LR
    REQ["incoming payload"] --> GUARD["guardrails.inspect<br/>app/guardrails.py"]
    GUARD -->|"ok"| ADMIT["Gateway.handle<br/>tenant cap"]
    GUARD -->|"400"| REJECT["model_not_allowed · bad_max_tokens<br/>prompt_too_long · empty_prompt"]
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
