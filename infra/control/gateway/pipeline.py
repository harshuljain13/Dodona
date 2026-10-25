"""The order one request goes through: gateway, then overflow only if it shed.

    gateway.handle   admission -> placement -> prefill -> KV hop -> decode
    overflow         only on a capacity shed
    record_outcome   one trace line and the latencies, either way

Written here once so serve.py, the REPL, the harness and the smoke scripts all
run the same path. Depends on both sides; neither side depends on this.
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

    `gateway` is anything with `.handle(req) -> Response`. `backup` replaces the
    HTTP burst so tests never pay. `budget` is the lifetime overflow cap.
    """
    started = time.perf_counter()
    response = gateway.handle(req)
    local_ms = _elapsed_ms(started)

    # A 500 is a transient engine error, not a capacity signal: retry locally.
    if response.status == 500:
        retry_started = time.perf_counter()
        response = gateway.handle(req)
        local_ms += _elapsed_ms(retry_started)
        return record_outcome(req, response, via="local", local_ms=local_ms)

    # Only a capacity shed leaves. A 429 is the tenant over quota and slice_oom
    # does not fit anywhere, so paying a backup for either is just overspending.
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
    """One word for the report, keeping different kinds of refusal apart."""
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
