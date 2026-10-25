from __future__ import annotations

from gateway.types import Handoff, Request


def kv_params(transfer_id: str, *, producer: bool,
              bootstrap_addr: str = "", engine_id: str = "") -> dict:
    """The two halves of vLLM's disaggregated handshake.

    Mooncake is a transport, not a lookup service: decode cannot find what
    prefill stored unless something names it. `transfer_id` is that name, and it
    has to be identical on both legs. The consumer additionally names which
    producer to pull from, by bootstrap address and engine id.

    Shape taken from vLLM's own proxy,
    examples/disaggregated/mooncake_connector/mooncake_connector_proxy.py.
    Omit any of it and the pod logs "Missing transfer_id in kv_transfer_params
    from router!" and answers every lookup with a miss.
    """
    if producer:
        return {"do_remote_decode": True, "do_remote_prefill": False,
                "transfer_id": transfer_id}
    out = {"do_remote_decode": False, "do_remote_prefill": True,
           "transfer_id": transfer_id}
    if bootstrap_addr:
        out["remote_bootstrap_addr"] = bootstrap_addr
    if engine_id:
        out["remote_engine_id"] = engine_id
    return out


def enqueue(handoff: Handoff, req: Request) -> tuple[dict | None, dict | None]:
    """Run one request across the handoff: prefill, hop, decode.

    When src == dst there is no hop to make, and asking for one would have the
    pod transfer to itself.
    """
    if handoff.decode is handoff.prefill:
        out = handoff.prefill.enqueue(req, phase="both")
        return None, out if isinstance(out, dict) else None

    # Both legs carry the same id, which is the whole point.
    transfer_id = f"xfer-{req.id}"
    bootstrap = getattr(handoff.prefill, "bootstrap_url", "") or ""
    engine_id = ""
    if hasattr(handoff.prefill, "engine_id"):
        engine_id = handoff.prefill.engine_id() or ""

    out_p = handoff.prefill.enqueue(
        req, phase="prefill",
        kv_transfer_params=kv_params(transfer_id, producer=True))

    hop = None
    bus = getattr(handoff.prefill, "bus", None) or getattr(handoff.decode, "bus", None)
    if bus is not None and hasattr(bus, "transfer"):
        hop = bus.transfer(handoff.prefill.id, handoff.decode.id, req)

    out_d = handoff.decode.enqueue(
        req, phase="decode",
        kv_transfer_params=kv_params(transfer_id, producer=False,
                                     bootstrap_addr=bootstrap, engine_id=engine_id))

    completion = out_d if isinstance(out_d, dict) else out_p if isinstance(out_p, dict) else None
    return hop, completion
