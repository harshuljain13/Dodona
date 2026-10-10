from __future__ import annotations

import os
from dataclasses import dataclass, field

MAX_NEW_TOKENS = int(os.environ.get("MAX_NEW_TOKENS") or 512)

#: Both of these were constants, and both silently refuse the workload an admission
#: experiment needs. A prompt at a 7B's KV crossover on an A100-40 is ~33,000 CHARACTERS,
#: so an 8,000 limit turns every request into 400 prompt_too_long — a refusal by a
#: guardrail that reads, in any report, exactly like a refusal by the policy.
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS") or 8_000)

_ALIASES = frozenset({"text", "vision"})

@dataclass(frozen=True)
class Guard:
    ok: bool
    status: int = 200
    reason: str = "ok"
    message: str = ""
    payload: dict = field(default_factory=dict)

def allowed_models() -> set[str]:
    text = os.environ.get("TEXT_MODEL") or os.environ.get("LOCAL_MODEL") or "Qwen/Qwen2.5-3B-Instruct"
    vision = os.environ.get("VISION_MODEL") or "Qwen/Qwen2.5-VL-3B-Instruct"
    # SERVED_NAMES carries every --served-model-name the engine answers to. Without it the
    # allowlist holds a model's full id but not its alias, so a client using the short
    # name gets 400 model_not_allowed from the gateway for a request the engine behind it
    # would have served.
    extra = {n.strip() for n in (os.environ.get("SERVED_NAMES") or "").split(",") if n.strip()}
    return set(_ALIASES) | {text, vision} | extra

def inspect(payload: dict) -> Guard:
    body = dict(payload)
    model = str(body.get("model") or "")
    if model not in allowed_models():
        return Guard(False, 400, "model_not_allowed", f"model {model!r} not in local vLLM allowlist", body)

    tokens = body.get("max_tokens", body.get("max_new_tokens"))
    if tokens is None:
        body["max_tokens"] = MAX_NEW_TOKENS
        tokens = MAX_NEW_TOKENS
    if tokens is not None:
        try:
            n = int(tokens)
        except (TypeError, ValueError):
            return Guard(False, 400, "bad_max_tokens", "max_tokens must be an int", body)
        if n <= 0:
            return Guard(False, 400, "bad_max_tokens", "max_tokens must be > 0", body)
        if n > MAX_NEW_TOKENS:
            body["max_tokens"] = MAX_NEW_TOKENS
            body.pop("max_new_tokens", None)

    text = _message_text(body)
    if len(text) > MAX_PROMPT_CHARS:
        return Guard(False, 400, "prompt_too_long", f"prompt > {MAX_PROMPT_CHARS} chars", body)
    if not _is_vision(model, body) and not text.strip():
        return Guard(False, 400, "empty_prompt", "text model needs a prompt", body)
    if _is_vision(model, body) and not _has_image(body):
        return Guard(False, 400, "vision_needs_image", "vision model needs an image part", body)

    body.setdefault("tenant", "lab")
    return Guard(True, payload=body)

def _is_vision(model: str, payload: dict) -> bool:
    raw = model.lower()
    if raw == "vision" or "-vl" in raw or "vision" in raw:
        return True
    return _has_image(payload)

def _message_text(payload: dict) -> str:
    parts: list[str] = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    if payload.get("prompt"):
        parts.append(str(payload["prompt"]))
    return "\n".join(parts)

def _has_image(payload: dict) -> bool:
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("image_url", "image"):
                return True
    return False
