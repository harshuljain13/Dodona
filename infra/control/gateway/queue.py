from __future__ import annotations

from gateway.types import Handoff, Request

def enqueue(handoff: Handoff, req: Request) -> tuple[dict | None, dict | None]:

    if handoff.decode is handoff.prefill:
        out = handoff.prefill.enqueue(req, phase="both")
        return None, out if isinstance(out, dict) else None
    out_p = handoff.prefill.enqueue(req, phase="prefill")
    hop = None
    bus = getattr(handoff.prefill, "bus", None) or getattr(handoff.decode, "bus", None)
    if bus is not None and hasattr(bus, "transfer"):
        hop = bus.transfer(handoff.prefill.id, handoff.decode.id, req)
    out_d = handoff.decode.enqueue(req, phase="decode")
    completion = out_d if isinstance(out_d, dict) else out_p if isinstance(out_p, dict) else None
    return hop, completion
