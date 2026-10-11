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
