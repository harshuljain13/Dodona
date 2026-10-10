from __future__ import annotations

from gateway.types import estimate_prompt_tokens, request_from_openai

def test_openai_messages_become_prompt_and_token_estimate() -> None:
    payload = {
        "model": "text",
        "messages": [{"role": "user", "content": "x" * 80}],
        "max_tokens": 24,
    }
    req = request_from_openai(payload)
    assert req.prompt == "x" * 80
    assert req.messages == payload["messages"]
    assert req.max_new_tokens == 24
    assert req.prompt_tokens == 20
    assert req.prefix_hash
    assert estimate_prompt_tokens({"prompt_tokens": 9}) == 9

def test_vision_payload_keeps_image_parts() -> None:
    payload = {
        "model": "Qwen2.5-VL",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
                ],
            }
        ],
    }
    req = request_from_openai(payload)
    assert req.prompt == "What color?"
    assert req.messages is not None
    assert req.messages[0]["content"][1]["type"] == "image_url"

def test_forward_allowlist_keeps_tools_and_drops_control_keys() -> None:
    """`tools` must reach the engine; our own control keys must not.

    Without `tools` the model has no schemas, answers in prose, emits no
    tool_calls, and the agent loop ends after one turn — which looks like a
    weak model and is a dropped field.
    """
    req = request_from_openai({
        "model": "text",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "web_search"}}],
        "tool_choice": "auto",
        "temperature": 0.3,
        # ours, and a 400 from vLLM if forwarded
        "kv_hop": True, "prefix_hash": "abc", "tenant": "lab", "priority": 2,
    })
    assert req.extra is not None
    assert req.extra["tools"][0]["function"]["name"] == "web_search"
    assert req.extra["tool_choice"] == "auto"
    assert req.extra["temperature"] == 0.3
    for ours in ("kv_hop", "prefix_hash", "tenant", "priority"):
        assert ours not in req.extra


def test_prefill_leg_asks_for_one_token() -> None:
    """The prefill leg is one forward pass, not a full generation.

    Before this, `phase` was ignored and both legs sent the full max_tokens —
    so the prefill worker generated an answer that queue.py threw away.
    """
    import router.pools as pools

    sent: list[dict] = []

    class Spy(pools.VLLMWorker):
        def _post(self, path: str, payload: dict) -> str:  # type: ignore[override]
            sent.append(payload)
            return '{"choices":[{"message":{"content":"x"}}]}'

    req = request_from_openai({
        "model": "text",
        "messages": [{"role": "user", "content": "x" * 400}],
        "max_tokens": 256,
    })
    w = Spy("p0", "http://127.0.0.1:8000")
    w.enqueue(req, phase="prefill")
    w.enqueue(req, phase="decode")
    w.enqueue(req, phase="both")

    assert sent[0]["max_tokens"] == 1, "prefill leg must be a single forward pass"
    assert sent[1]["max_tokens"] == 256, "decode leg does the real generation"
    assert sent[2]["max_tokens"] == 256, "src == dst does the whole request"
