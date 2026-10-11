from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Protocol

STALE_S = 5.0

KV_SATURATION = 0.80

STICKY_OVERLAP = 0.8
STICKY_TAU = 10_000

PLAN_HIGH_WATER = 0.7

#: Keys passed through to vLLM verbatim.
#:
#: An allowlist rather than forwarding the payload wholesale, in both directions.
#: The gateway's own control keys (kv_hop, prefix_hash, tenant, priority, ...) are
#: ours and mean nothing to the engine, which answers an unknown key with a 400.
#: And `model`, `messages` and `max_tokens` are set by the worker, because the
#: token limit is a capacity decision the gateway owns.
#:
#: `tools` is the one that matters. Without it a tool-calling agent gets no
#: schemas, answers from its own weights in prose, emits no tool_calls, and its
#: loop ends after one turn — which looks like a weak model rather than a dropped
#: field. `chat_template_kwargs` carries `enable_thinking: false`; Qwen3's chat
#: template defaults to thinking mode and will otherwise spend the whole token
#: budget inside a <think> block.
FORWARD = frozenset(
    {
        "tools",
        "tool_choice",
        "chat_template_kwargs",
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "repetition_penalty",
        "response_format",
        "seed",
    }
)

def forwardable(payload: dict) -> dict:
    """The subset of an inbound payload that belongs to the engine.

    Absent keys stay absent rather than becoming defaults: sending
    `temperature: None` is not the same as not sending it, and vLLM treats the
    two differently.
    """
    return {k: payload[k] for k in FORWARD if k in payload and payload[k] is not None}

@dataclass
class Request:
    id: str
    arrival_t: float
    priority: int
    prompt_tokens: int
    max_new_tokens: int
    prefix_hash: str | None
    timeout_s: float
    tenant: str
    aborted: bool = False

    capability: str = "text"
    prompt: str = ""
    messages: list | None = None
    #: Engine-bound sampling and tool keys, already filtered through FORWARD.
    extra: dict | None = None
    force_phase: bool = False
    evict_after: bool = False

def request_messages(req: Request) -> list:
    if req.messages:
        return req.messages
    return [{"role": "user", "content": req.prompt or req.id}]

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

def estimate_prompt_tokens(payload: dict) -> int:
    if payload.get("prompt_tokens") is not None:
        return max(1, int(payload["prompt_tokens"]))
    text = _message_text(payload)
    return max(16, max(1, len(text) // 4))

def request_from_openai(payload: dict) -> Request:
    text = _message_text(payload)
    prefix = payload.get("prefix_hash") or (
        hashlib.sha256(text[:256].encode("utf-8")).hexdigest()[:16] if text else None
    )
    return Request(
        # Unique per request, NOT a constant. queue.py derives the KV handshake's
        # transfer_id from this, so a shared id makes concurrent requests claim the
        # same transfer. The engine then gets a finished_recving for an id whose
        # earlier owner has already completed and had its blocks freed, and
        # scheduler.py's `assert req_id in self.requests` kills EngineCore. This
        # defaulted to the literal "http", so every OpenAI client that omits an id —
        # which is all of them, load.py included — sent every request as "http" and
        # took decode down at 8 concurrent sessions.
        id=str(payload.get("id") or payload.get("user") or f"req-{uuid.uuid4().hex[:16]}"),
        arrival_t=float(payload.get("arrival_t") or 0.0),
        priority=int(payload.get("priority", 1)),
        prompt_tokens=estimate_prompt_tokens(payload),
        max_new_tokens=int(payload.get("max_tokens", payload.get("max_new_tokens", 512))),
        prefix_hash=prefix,
        timeout_s=float(payload.get("timeout_s", 30)),
        tenant=str(payload.get("tenant") or payload.get("user") or "lab"),
        capability=str(payload.get("capability") or ""),
        prompt=text,
        messages=payload.get("messages") if isinstance(payload.get("messages"), list) else None,
        extra=forwardable(payload) or None,
        force_phase=bool(payload.get("kv_hop")),
        evict_after=bool(payload.get("kv_hop") and payload.get("kv_evict")),
    )

@dataclass
class Snapshot:
    pod_id: str
    age_s: float
    healthy: bool = True
    saturating: bool = False
    kv_free_ratio: float | None = None
    tokens_in_flight: int | None = None
    uncached_prefill_tokens: int | None = None
    active_requests: int | None = None
    queue_depth: int | None = None
    waiting: int | None = None
    running: int | None = None
    prefix_tokens: dict[str, int] | None = None

    soak_weight: float = 1.0

@dataclass
class Shed:

    status: int
    error: str
    reason: str = "no_eligible_pod"
    retry_after_s: float | None = None
    message: str = ""

    def to_response(self) -> Response:
        headers: dict[str, str] = {}
        if self.status in (503, 529):
            headers["Retry-After"] = str(int(self.retry_after_s or 2))
        return Response(
            status=self.status,
            error=self.error,
            reason=self.reason,
            headers=headers,
            body={"error": {"type": self.error, "message": self.message}},
        )

@dataclass
class Handoff:
    prefill: Worker
    decode: Worker

@dataclass
class Response:
    status: int
    error: str | None = None
    reason: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: dict = field(default_factory=dict)
    handoff: Handoff | None = None

    via: str = "local"
    local_status: int | None = None

class SliceOOM(Exception):
    pass

class Worker(Protocol):
    id: str

    def enqueue(self, req: Request, phase: str = "both") -> dict | None: ...
    def snapshot(self) -> Snapshot: ...
    def abort(self, req_id: str) -> None: ...
