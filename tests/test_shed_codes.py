from __future__ import annotations

from router.router import Router
from gateway.admission import Gateway
from router.kvbus import KVBus
from router.pools import FakeWorker
from gateway.types import Request

def _req(**kw) -> Request:
    base = dict(
        id="r0",
        arrival_t=0.0,
        priority=1,
        prompt_tokens=32,
        max_new_tokens=8,
        prefix_hash=None,
        timeout_s=30.0,
        tenant="lab",
    )
    base.update(kw)
    return Request(**base)

def test_tenant_over_cap_is_429_and_epp_does_not_run() -> None:
    bus = KVBus()
    prefill = [FakeWorker("p0", bus=bus)]
    decode = [FakeWorker("d0", bus=bus)]
    epp = Router(prefill, decode)
    called = {"n": 0}
    orig = epp.place

    def wrapped(req: Request):
        called["n"] += 1
        return orig(req)

    epp.place = wrapped
    gw = Gateway(epp, tokens_per_min=20)
    resp = gw.handle(_req(prompt_tokens=64, max_new_tokens=8))
    assert resp.status == 429
    assert resp.error == "rate_limit_error"
    assert called["n"] == 0
    assert resp.body["error"]["type"] == "rate_limit_error"

def test_both_pools_saturating_is_503_with_retry_after_no_cross_hop() -> None:
    bus = KVBus()
    prefill = [FakeWorker("p0", bus=bus), FakeWorker("p1", bus=bus)]
    decode = [FakeWorker("d0", bus=bus), FakeWorker("d1", bus=bus)]
    for w in prefill + decode:
        w.saturating = True
    epp = Router(prefill, decode)
    gw = Gateway(epp)
    resp = gw.handle(_req())
    assert resp.status == 503
    assert resp.error == "server_is_overloaded"
    assert "Retry-After" in resp.headers

    assert all(not w._reqs for w in prefill + decode)

def test_prefill_saturating_does_not_bind_on_decode_pool() -> None:
    bus = KVBus()
    prefill = [FakeWorker("p0", bus=bus)]
    decode = [FakeWorker("d0", bus=bus)]
    prefill[0].saturating = True
    epp = Router(prefill, decode)
    result = epp.place(_req())
    assert getattr(result, "status", None) == 503
    assert not decode[0]._reqs


def test_the_rate_limit_window_advances_on_the_servers_own_clock() -> None:
    """"Tokens per minute" has to mean per minute.

    The limiter used to window on `req.arrival_t`, which request_from_openai
    defaults to 0.0 when the client omits it — and every real client omits it.
    `0.0 - 0.0` is never >= 60, so the window never rolled over and `used`
    accumulated for the life of the process: a 2,000,000 tokens/min cap was
    really 2,000,000 tokens ever, after which the gateway 429'd everything.

    In the capacity sweep that was indistinguishable from the cluster refusing
    load, and it silently capped the measurement at 2 sessions.
    """
    bus = KVBus()
    epp = Router([FakeWorker("p0", bus=bus)], [FakeWorker("d0", bus=bus)])

    now = [100.0]
    gw = Gateway(epp, tokens_per_min=1_000, clock=lambda: now[0])

    def ask() -> int:
        return gw.handle(_req(prompt_tokens=600, max_new_tokens=100)).status

    assert ask() == 200, "first request is inside the budget"
    assert ask() == 429, "second exhausts 1,000 tokens and must be refused"

    now[0] += 61.0           # a minute later
    assert ask() == 200, (
        "the window must have rolled over — if this 429s, 'per minute' is a "
        "lifetime quota and the gateway is permanently closed")


def test_an_idle_pod_with_a_warm_cache_is_not_saturated() -> None:
    """Retained prefix-cache blocks are not committed memory.

    vllm:kv_cache_usage_perc counts blocks the prefix cache is holding, and vLLM
    evicts those on demand. Measured on this cluster: prefill reported 0.736
    usage with 0 running and 0 waiting — a pod able to accept anything, which
    the old rule (shed below 0.20 free) refused outright.

    That produced 6,100 kv_free 503s, 503s at 24 sessions while decode sat at
    50%, and three benchmark cases that could not run at all because the warm-up
    request was refused by an empty cluster.

    Saturation comes from the engine now: num_requests_waiting_by_reason with
    reason="capacity".
    """
    from gateway.types import Snapshot
    from router.router import is_saturating

    idle_but_cached = Snapshot(pod_id="p0", age_s=0.0, healthy=True,
                               saturating=False, kv_free_ratio=1.0 - 0.736)
    assert not is_saturating(idle_but_cached), (
        "a pod holding evictable cache was treated as full")

    out_of_room = Snapshot(pod_id="p0", age_s=0.0, healthy=True,
                           saturating=True, kv_free_ratio=1.0 - 0.736)
    assert is_saturating(out_of_room), "the engine said it had no room"

    really_full = Snapshot(pod_id="p0", age_s=0.0, healthy=True,
                           saturating=False, kv_free_ratio=0.01)
    assert is_saturating(really_full), "the backstop must still catch real exhaustion"


def test_the_capacity_signal_reaches_the_snapshot() -> None:
    """parse_vllm_metrics must surface reason="capacity", or the fix is inert."""
    from router.pools import parse_vllm_metrics

    text = (
        'vllm:kv_cache_usage_perc{engine="0"} 0.736\n'
        'vllm:num_requests_running{engine="0"} 0.0\n'
        'vllm:num_requests_waiting{engine="0"} 0.0\n'
        'vllm:num_requests_waiting_by_reason{engine="0",reason="capacity"} 0.0\n'
        'vllm:num_requests_waiting_by_reason{engine="0",reason="deferred"} 3.0\n'
    )
    assert parse_vllm_metrics(text)["at_capacity"] == 0.0, (
        "deferred is not capacity — counting it would shed on LoRA and KV-transfer waits")
    busy = text.replace('reason="capacity"} 0.0', 'reason="capacity"} 7.0')
    assert parse_vllm_metrics(busy)["at_capacity"] == 7.0
