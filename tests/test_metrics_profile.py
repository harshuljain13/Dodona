from __future__ import annotations

from gateway.admission import Gateway
from gateway.metrics import METRICS
from router.pools import FakeWorker
from router.router import Router
from gateway.types import Request

def _req(**kw) -> Request:
    base = dict(
        id="m",
        arrival_t=0.0,
        priority=1,
        prompt_tokens=16,
        max_new_tokens=8,
        prefix_hash="p",
        timeout_s=30.0,
        tenant="lab",
    )
    base.update(kw)
    return Request(**base)

def test_render_keeps_day1_names_and_adds_profile_series() -> None:
    text = METRICS.render()
    assert "orch_requests_total 0" in text
    assert "orch_kv_evict_total 0" in text
    assert "orch_sticky_total 0" in text
    assert "orch_place_total" in text
    assert 'orch_request_duration_seconds_bucket{stage="gateway",le="+Inf"} 0' in text
    assert 'orch_request_duration_seconds_bucket{stage="pick",le="+Inf"} 0' in text
    assert 'orch_replica_healthy{pool="prefill",pod="none"} 0' in text

def test_handle_profiles_gateway_and_fills_replica_gauges() -> None:
    prefill = FakeWorker("prefill-0")
    decode = FakeWorker("decode-0")
    gw = Gateway(Router([prefill], [decode]))
    resp = gw.handle(_req())
    assert resp.status == 200
    text = METRICS.render()
    assert "orch_requests_total 1" in text
    assert "orch_completed_total 1" in text
    assert 'orch_place_total{capability="text"} 1' in text
    assert METRICS.latency["gateway"].count == 1
    assert METRICS.latency["pick"].count >= 1
    assert 'orch_replica_healthy{pool="prefill",pod="prefill-0"} 1' in text
    assert 'orch_replica_healthy{pool="decode",pod="decode-0"} 1' in text
    assert "orch_replica_kv_free_ratio" in text
    assert "orch_tokens_in_flight" in text

def test_shed_still_records_gateway_latency() -> None:
    gw = Gateway(Router([], []), tokens_per_min=1)
    resp = gw.handle(_req(prompt_tokens=64, max_new_tokens=64))
    assert resp.status == 429
    assert METRICS.latency["gateway"].count == 1
    assert "orch_shed_total" in METRICS.render()
    summary = "\n".join(METRICS.profile_summary())
    assert "gateway" in summary
    assert "requests=1" in summary
