"""What happens to one request, in order.

    1. gateway.handle     tenant window -> admission -> placement
                          -> prefill -> KV hop -> decode
    2. overflow           ONLY if the cluster shed for lack of capacity
    3. record_outcome     one trace line and the latency metrics, either way

serve.py owns this sequence; it lives here so the REPL, the harness and the
smoke scripts exercise exactly the same path rather than three near-copies.

Why it is shaped this way. `router/overflow.py` used to sit at the TOP of this:
it held the gateway, called it, and emitted the trace for every request
including the ones that never overflowed. Reading serve.py you saw
"guardrails, then overflow" and could not tell that admission, placement and
the hop happened at all — our own control plane looked like an implementation
detail of a relief valve used at most a few times per process. It is the other
way round, and now it reads that way.

overflow.py still imports nothing from the control plane. The dependency only
points one way: this module knows about both, neither knows about this one.
"""
from __future__ import annotations

import time
from typing import Any

from gateway.metrics import METRICS
from gateway.types import Request, Response
from router import overflow as overflow_module
from router.overflow import OverflowBudget, OverflowPort
from router.trace import TRACES


def serve_request(
    req: Request,
    gateway: Any,
    *,
    backup: OverflowPort | None = None,
    budget: OverflowBudget | None = None,
) -> Response:
    """Run one request through the cluster, then record what happened.

    `gateway` is anything with `.handle(req) -> Response` — duck-typed so this
    works against the real Gateway, the REPL's, and a stub.
    `backup` short-circuits the HTTP call, so tests never make a paid request.
    `budget` carries the lifetime overflow cap; a fresh one means uncapped.
    """
    started = time.perf_counter()
    response = gateway.handle(req)
    local_ms = _elapsed_ms(started)

    # A 500 is a transient engine error rather than a capacity signal, so it
    # gets one more try on the same cluster before anything leaves.
    if response.status == 500:
        retry_started = time.perf_counter()
        response = gateway.handle(req)
        local_ms += _elapsed_ms(retry_started)
        return record_outcome(req, response, via="local", local_ms=local_ms)

    # Only a genuine capacity shed leaves. A 429 is the tenant being over its
    # own quota — paying a backup for a request we are refusing on quota
    # grounds is overspending, not a capacity event. slice_oom means the
    # request does not fit anywhere, so a backup would not help either.
    if response.status in (503, 529) and response.error != "slice_oom":
        return _escalate(req, response, local_ms=local_ms, backup=backup, budget=budget)

    return record_outcome(req, response, via="local", local_ms=local_ms)


def _escalate(
    req: Request,
    local_response: Response,
    *,
    local_ms: float,
    backup: OverflowPort | None,
    budget: OverflowBudget | None,
) -> Response:
    """Try a backup endpoint. Falls back to the local shed if there is none."""
    budget = budget or OverflowBudget()
    if not budget.allows():
        return record_outcome(req, local_response, via="local", local_ms=local_ms)
    if backup is None and not overflow_module.is_configured():
        return record_outcome(req, local_response, via="local", local_ms=local_ms)

    budget.charge()
    METRICS.inc_overflow()
    started = time.perf_counter()
    if backup is not None:
        response, backend = backup.complete(req), None
    else:
        response, backend = overflow_module.call_backup(req), overflow_module.backend_name()
    return record_outcome(
        req,
        response,
        via="overflow",
        local_status=local_response.status,
        local_reason=local_response.reason,
        local_ms=local_ms,
        overflow_ms=_elapsed_ms(started),
        backend=backend,
    )


def record_outcome(
    req: Request,
    resp: Response,
    *,
    via: str,
    local_status: int | None = None,
    local_reason: str | None = None,
    backend: str | None = None,
    local_ms: float = 0.0,
    overflow_ms: float | None = None,
) -> Response:
    """Stamp the response, observe the latencies, write one trace line."""
    resp.via = via
    resp.local_status = local_status
    resp.headers["X-Via"] = via
    resp.body["via"] = via
    if local_status is not None:
        resp.body["local_status"] = local_status

    total_ms = local_ms + (overflow_ms or 0.0)
    METRICS.observe_duration("local", local_ms / 1000.0)
    METRICS.observe_duration("e2e", total_ms / 1000.0)
    if overflow_ms is not None:
        METRICS.observe_duration("overflow", overflow_ms / 1000.0)

    event = {
        "id": req.id,
        "tenant": req.tenant,
        "ts": time.time(),
        "via": via,
        "happened": _what_happened(via, resp, local_status),
        "status": resp.status,
        "error": resp.error,
        "reason": local_reason or resp.reason,
        "local_ms": round(local_ms, 3),
        "total_ms": round(total_ms, 3),
    }
    if local_status is not None:
        event["local_status"] = local_status
    if overflow_ms is not None:
        event["overflow_ms"] = round(overflow_ms, 3)
    if backend:
        event["backend"] = backend
    event.update(_completion_fields(resp.body))
    if isinstance(resp.body.get("kv_hop"), dict):
        event["kv_hop"] = resp.body["kv_hop"]
    TRACES.record(event)
    return resp


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _what_happened(via: str, resp: Response, local_status: int | None) -> str:
    """One word for the report. Distinguishes refusals that mean different things."""
    if via == "overflow":
        return "overflow_ok" if resp.status == 200 else "overflow_error"
    if resp.status == 200:
        return "served_local"
    if resp.status == 429:
        return "tenant_refuse"
    if resp.error == "slice_oom":
        return "refuse"
    if (local_status or resp.status) in (503, 529):
        return "shed_no_leave"
    return "local_error"


def _completion_fields(body: dict) -> dict:
    """The parts of an OpenAI completion worth keeping in a trace."""
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    text = ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = message.get("content") or first.get("text") or ""

    fields: dict = {}
    if body.get("model"):
        fields["model"] = body["model"]
    if body.get("id") and body["id"] != "overflow-fake":
        fields["completion_id"] = body["id"]
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if usage.get(key) is not None:
            fields[key] = usage[key]
    if isinstance(text, str) and text:
        fields["text"] = text[:160]
    return fields
