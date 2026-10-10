from __future__ import annotations

from pathlib import Path

from edge.guardrails import MAX_NEW_TOKENS, inspect
from edge.harness import BEHAVIORS, run_behavior
from gateway.serve import listed_models, prepare_chat, sse_from_completion

ROOT = Path(__file__).resolve().parents[1]

def test_unknown_cloud_model_refuses() -> None:
    g = inspect({"model": "some-cloud-model", "messages": [{"role": "user", "content": "hi"}]})
    assert not g.ok and g.status == 400 and g.reason == "model_not_allowed"

def test_local_vllm_names_pass() -> None:
    for model in ("text", "Qwen/Qwen2.5-3B-Instruct"):
        g = inspect({"model": model, "messages": [{"role": "user", "content": "GPU?"}], "max_tokens": 9_999})
        assert g.ok, model
        assert g.payload["max_tokens"] == MAX_NEW_TOKENS
        assert g.payload["tenant"] == "lab"

def test_empty_prompt_and_vision_without_image() -> None:
    empty = inspect({"model": "text", "messages": [{"role": "user", "content": "  "}]})
    assert not empty.ok and empty.reason == "empty_prompt"
    vision = inspect({"model": "vision", "messages": [{"role": "user", "content": "color?"}]})
    assert not vision.ok and vision.reason == "vision_needs_image"
    vl = inspect({"model": "Qwen/Qwen2.5-VL-3B-Instruct", "messages": [{"role": "user", "content": "color?"}]})
    assert not vl.ok and vl.reason == "vision_needs_image"

def test_vision_with_image_passes() -> None:
    g = inspect(
        {
            "model": "vision",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "color?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
                    ],
                }
            ],
        }
    )
    assert g.ok

def test_all_local_harness_behaviors() -> None:
    for name in BEHAVIORS:
        result = run_behavior(name)
        assert result.passed, result.summary

def test_orch_lists_only_aliases_it_can_serve(monkeypatch) -> None:
    """A listed model that always 400s is worse than one that is absent."""
    monkeypatch.delenv("VISION_MODEL", raising=False)
    monkeypatch.setenv("TEXT_MODEL", "Qwen/Qwen2.5-7B-Instruct-AWQ")
    ids = {row["id"] for row in listed_models()["data"]}
    assert "text" in ids and "Qwen/Qwen2.5-7B-Instruct-AWQ" in ids
    assert "vision" not in ids, "no VISION_MODEL deployed, so do not advertise it"

    monkeypatch.setenv("VISION_MODEL", "Qwen/Qwen2.5-VL-3B-Instruct")
    assert "vision" in {row["id"] for row in listed_models()["data"]}

def test_sse_from_completion_is_openai_stream() -> None:
    raw = sse_from_completion(
        {"id": "r1", "model": "text", "choices": [{"message": {"content": "hello GPU"}}]}
    ).decode()
    assert raw.startswith("data: ")
    assert "[DONE]" in raw
    assert "hello GPU" in raw
    assert "chat.completion.chunk" in raw

def test_orch_serve_applies_edge_before_admit() -> None:
    status, body = prepare_chat({"model": "some-cloud-model", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 400 and body["error"]["type"] == "model_not_allowed"
    status, body = prepare_chat({"model": "text", "messages": [{"role": "user", "content": "GPU?"}]})
    assert status == 200 and body["tenant"] == "lab" and body["model"] == "text"

def test_app_edge_has_no_openai_account_or_litellm() -> None:
    for name in ("guardrails.py", "harness.py", "crew_flood.py"):
        text = (ROOT / "control" / "edge" / name).read_text().lower()
        assert "litellm" not in text
        assert "sk-lab" not in text
        assert "force 503" not in text
    guard = (ROOT / "control" / "edge" / "guardrails.py").read_text()
    harness = (ROOT / "control" / "edge" / "harness.py").read_text()
    for src in (guard, harness):
        assert "from gateway" not in src
        assert "import gateway" not in src
        assert "orch_" not in src
