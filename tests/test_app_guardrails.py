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
    edge = [ROOT / "infra" / "control" / "edge" / n for n in ("guardrails.py", "harness.py")]
    for path in edge + [ROOT / "benchmarking" / "crew_flood.py"]:
        text = path.read_text().lower()
        assert "litellm" not in text
        assert "sk-lab" not in text
        assert "force 503" not in text
    guard = (ROOT / "infra" / "control" / "edge" / "guardrails.py").read_text()
    harness = (ROOT / "infra" / "control" / "edge" / "harness.py").read_text()
    for src in (guard, harness):
        assert "from gateway" not in src
        assert "import gateway" not in src
        assert "orch_" not in src


def test_the_prompt_cap_follows_the_engine_not_a_constant() -> None:
    """8,000 characters refused the agent mid-research, after it had said so aloud.

    `scrape_url` returns a page as markdown, and that page is part of the NEXT
    model call. So the second hop of any real research turn passed 8,000 chars
    and came back 400 prompt_too_long — from our own guard, on a fleet whose
    engine was started with a 16,384-token context it never got to use.

    The cap is derived from MAX_MODEL_LEN now. This test pins that, because the
    file carried a comment explaining the bug while the default kept causing it.
    """
    import importlib
    import os
    import sys

    sys.path.insert(0, str(ROOT / "infra" / "control"))
    import edge.guardrails as guardrails

    saved = {k: os.environ.get(k) for k in ("MAX_MODEL_LEN", "MAX_PROMPT_CHARS")}
    try:
        for key in saved:
            os.environ.pop(key, None)

        os.environ["MAX_MODEL_LEN"] = "16384"
        importlib.reload(guardrails)
        assert guardrails.MAX_PROMPT_CHARS > 8_000, (
            "the default is back to a constant below what a research turn needs")
        # A scraped page plus the conversation around it.
        assert guardrails.MAX_PROMPT_CHARS >= 60_000, (
            f"{guardrails.MAX_PROMPT_CHARS} chars is under a 16k-token context")

        # A narrower engine must narrow the guard with it — the point of deriving.
        os.environ["MAX_MODEL_LEN"] = "4096"
        importlib.reload(guardrails)
        narrow = guardrails.MAX_PROMPT_CHARS
        os.environ["MAX_MODEL_LEN"] = "16384"
        importlib.reload(guardrails)
        assert narrow < guardrails.MAX_PROMPT_CHARS, (
            "the cap ignores MAX_MODEL_LEN, so it is a constant again")

        # An explicit value still wins: a capacity sweep needs to set it.
        os.environ["MAX_PROMPT_CHARS"] = "1234"
        importlib.reload(guardrails)
        assert guardrails.MAX_PROMPT_CHARS == 1234
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(guardrails)


def test_the_failed_turn_sentence_is_the_same_in_both_files() -> None:
    """A failed voice turn is SPOKEN, not signalled, so the words are the protocol.

    `pipecat_adapter._answer` catches the exception, pushes `voice.fallback_text`
    as a TextFrame and emits nothing structured — the browser receives an
    ordinary `said`. So the only way the UI can know a turn failed, and tell the
    listener to reconnect, is to recognise our own sentence.

    That makes app/dodona.yaml and page.tsx share a string literal across a
    language boundary, which is fragile enough to be worth a test: edit the
    spoken line, forget the constant, and the on-screen hint silently stops
    appearing on the one failure it exists for.
    """
    import re

    import yaml

    spec = yaml.safe_load((ROOT / "app" / "dodona.yaml").read_text())
    spoken = (spec.get("voice") or {}).get("fallback_text")
    assert spoken, "dodona.yaml no longer sets voice.fallback_text"

    page = (ROOT / "app" / "web" / "app" / "page.tsx").read_text()
    match = re.search(r'const FAILED_TURN = "([^"]+)"', page)
    assert match, "page.tsx no longer declares FAILED_TURN"

    assert spoken.startswith(match.group(1)), (
        f"page.tsx matches {match.group(1)!r} but the agent now says "
        f"{spoken!r} — the reconnect hint will never show")
