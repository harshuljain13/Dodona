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
