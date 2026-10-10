from __future__ import annotations

from router.router import Router
from gateway.admission import Gateway
from router.kvbus import KVBus
from gateway.metrics import METRICS
from router.pools import FakeWorker
from gateway.types import Request

def _req(**kw) -> Request:
    base = dict(
        id="kv",
        arrival_t=0.0,
        priority=1,
        prompt_tokens=32,
        max_new_tokens=8,
        prefix_hash="share",
        timeout_s=30.0,
        tenant="lab",
    )
    base.update(kw)
    return Request(**base)

def test_two_slices_handoff_records_mooncake_hop() -> None:
    bus = KVBus()
    prefill = FakeWorker("p0", bus=bus)
    decode = FakeWorker("d0", bus=bus)
    gw = Gateway(Router([prefill], [decode]))
    resp = gw.handle(_req())
    assert resp.status == 200
    hop = resp.body["kv_hop"]
    assert hop["src"] == "p0"
    assert hop["dst"] == "d0"
    assert hop["backend"] == "mooncake"
    assert hop["tokens"] == 32
    assert METRICS.kv_transfer_total == 1
    assert bus.cached("d0", "share") == 32
    assert len(bus.hops) == 1

def test_aggregated_same_engine_has_no_kv_hop() -> None:
    bus = KVBus()
    w = FakeWorker("w0", bus=bus)
    gw = Gateway(Router([w], [w]))
    resp = gw.handle(_req())
    assert resp.status == 200
    assert "kv_hop" not in resp.body
    assert METRICS.kv_transfer_total == 0
    assert bus.hops == []

def test_nccl_and_nixl_backends_are_empty_puts(monkeypatch) -> None:
    from router import nccl, nixl

    assert nccl.put({}) is None
    assert nixl.put({}) is None
    bus = KVBus()
    prefill = FakeWorker("p0", bus=bus)
    decode = FakeWorker("d0", bus=bus)
    monkeypatch.setenv("KV_BACKEND", "nccl")
    resp = Gateway(Router([prefill], [decode])).handle(_req())
    assert resp.body["kv_hop"]["backend"] == "nccl"
    monkeypatch.setenv("KV_BACKEND", "nixl")
    bus2 = KVBus()
    resp2 = Gateway(Router([FakeWorker("p1", bus=bus2)], [FakeWorker("d1", bus=bus2)])).handle(_req(id="kv2"))
    assert resp2.body["kv_hop"]["backend"] == "nixl"

def test_evict_drops_prefix_and_counts() -> None:
    bus = KVBus()
    bus.record("d0", "share", 32)
    assert bus.cached("d0", "share") == 32
    assert bus.evict("d0", "share") == 1
    assert bus.cached("d0", "share") == 0
    assert METRICS.kv_evict_total == 1
    assert "orch_kv_evict_total 1" in METRICS.render()
