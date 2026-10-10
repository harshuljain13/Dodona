from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from edge.guardrails import MAX_NEW_TOKENS, inspect

TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

@dataclass
class BehaviorResult:
    name: str
    passed: bool
    summary: str
    extra: dict = field(default_factory=dict)

def _text(prompt: str = "Write one sentence about a GPU.", **kw) -> dict:
    body = {"model": "text", "messages": [{"role": "user", "content": prompt}], "max_tokens": 32}
    body.update(kw)
    return body

def behavior_unknown_model() -> BehaviorResult:
    """A model outside the local allowlist is refused with 400."""
    g = inspect(_text(model="some-cloud-model"))
    passed = (not g.ok) and g.status == 400 and g.reason == "model_not_allowed"
    return BehaviorResult("unknown-model", passed, f"unknown-model  status={g.status} reason={g.reason}")

def behavior_local_qwen_alias() -> BehaviorResult:
    """The bare aliases text and vision resolve to the local models."""
    g = inspect(_text(model="Qwen/Qwen2.5-3B-Instruct"))
    passed = g.ok
    return BehaviorResult("local-qwen-alias", passed, f"local-qwen-alias  ok={g.ok}")

def behavior_clamp_max_tokens() -> BehaviorResult:
    """An oversized max_tokens is clamped to 512, not rejected."""
    g = inspect(_text(max_tokens=9_999))
    passed = g.ok and g.payload.get("max_tokens") == MAX_NEW_TOKENS
    return BehaviorResult(
        "clamp-max-tokens",
        passed,
        f"clamp-max-tokens  ok={g.ok} max_tokens={g.payload.get('max_tokens')}",
        {"max_tokens": g.payload.get("max_tokens")},
    )

def behavior_empty_prompt() -> BehaviorResult:
    """A text request with no text is refused before routing."""
    g = inspect(_text(""))
    passed = (not g.ok) and g.reason == "empty_prompt"
    return BehaviorResult("empty-prompt", passed, f"empty-prompt  status={g.status} reason={g.reason}")

def behavior_vision_needs_image() -> BehaviorResult:
    """A vision request with no image part is refused."""
    g = inspect({"model": "vision", "messages": [{"role": "user", "content": "What color?"}]})
    passed = (not g.ok) and g.reason == "vision_needs_image"
    return BehaviorResult("vision-needs-image", passed, f"vision-needs-image  status={g.status} reason={g.reason}")

def behavior_pass_text() -> BehaviorResult:
    """A well-formed text request passes every guardrail."""
    g = inspect(_text())
    passed = g.ok and g.payload.get("tenant") == "lab"
    return BehaviorResult("pass-text", passed, f"pass-text  ok={g.ok} tenant={g.payload.get('tenant')}")

BEHAVIORS: dict[str, Callable[[], BehaviorResult]] = {
    "unknown-model": behavior_unknown_model,
    "local-qwen-alias": behavior_local_qwen_alias,
    "clamp-max-tokens": behavior_clamp_max_tokens,
    "empty-prompt": behavior_empty_prompt,
    "vision-needs-image": behavior_vision_needs_image,
    "pass-text": behavior_pass_text,
}

def run_behavior(name: str) -> BehaviorResult:
    key = name.lower().replace("_", "-")
    if key not in BEHAVIORS:
        raise SystemExit(f"unknown behavior {name}; want {', '.join(BEHAVIORS)}")
    return BEHAVIORS[key]()

def _base() -> str:
    return (os.environ.get("ORCH_URL") or "http://127.0.0.1:8080/v1").rstrip("/")

def _http(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"error": raw}
        return exc.code, parsed

def live_chat() -> BehaviorResult:
    payload = _text()
    edge = inspect(payload)
    if not edge.ok:
        return BehaviorResult("live-chat", False, f"live-chat  edge refused {edge.reason}")
    status, body = _http("POST", f"{_base()}/chat/completions", edge.payload)
    passed = status == 200 and bool(body.get("choices") or body.get("via"))
    return BehaviorResult("live-chat", passed, f"live-chat  status={status}", {"status": status})

def live_vision() -> BehaviorResult:
    payload = {
        "model": "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color is this?"},
                    {"type": "image_url", "image_url": {"url": TINY_PNG}},
                ],
            }
        ],
        "max_tokens": 16,
    }
    edge = inspect(payload)
    if not edge.ok:
        return BehaviorResult("live-vision", False, f"live-vision  edge refused {edge.reason}")
    status, _body = _http("POST", f"{_base()}/chat/completions", edge.payload)
    passed = status == 200
    return BehaviorResult("live-vision", passed, f"live-vision  status={status}", {"status": status})

LIVE: dict[str, Callable[[], BehaviorResult]] = {
    "live-chat": live_chat,
    "live-vision": live_vision,
}

def main() -> None:
    parser = argparse.ArgumentParser(description="App harness — edge guardrails, optional live orch-serve :8080")
    parser.add_argument("--behavior", help="name from --list")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--live", action="store_true", help="POST /v1/chat/completions on orch-serve")
    args = parser.parse_args()
    suite = {**BEHAVIORS, **LIVE} if args.live else BEHAVIORS
    if args.list or not args.behavior:
        for name in suite:
            print(name)
        if not args.behavior:
            raise SystemExit(0)
    if args.behavior not in suite:
        raise SystemExit(f"unknown behavior {args.behavior}; want {', '.join(suite)}")
    result = suite[args.behavior]()
    print(result.summary)
    print("PASS" if result.passed else "FAIL")
    raise SystemExit(0 if result.passed else 1)

if __name__ == "__main__":
    main()
