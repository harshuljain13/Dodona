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


# ── the disaggregated handshake ────────────────────────────────────────────
#
# Without this, MooncakeConnector logs "Missing transfer_id in
# kv_transfer_params from router!" and answers every lookup with a miss. We ran
# 53,675 connector queries and got 0 hits before these existed.

def test_both_legs_carry_the_same_transfer_id() -> None:
    """The id IS the correlation. Different ids and the consumer finds nothing."""
    from gateway.queue import enqueue
    from gateway.types import Handoff

    class Spy:
        def __init__(self, name):
            self.id, self.bus, self.sent = name, KVBus(), []
            self.bootstrap_url = "http://vllm-prefill:8998"

        def engine_id(self):
            return "engine-abc"

        def enqueue(self, req, phase="both", kv_transfer_params=None):
            self.sent.append((phase, kv_transfer_params))
            return {"choices": []}

    prefill, decode = Spy("p0"), Spy("d0")
    enqueue(Handoff(prefill=prefill, decode=decode), _req(id="r1"))

    (_, p_kv), = prefill.sent
    (_, d_kv), = decode.sent
    assert p_kv["transfer_id"] == d_kv["transfer_id"] == "xfer-r1"
    assert p_kv["do_remote_decode"] is True and p_kv["do_remote_prefill"] is False
    assert d_kv["do_remote_prefill"] is True and d_kv["do_remote_decode"] is False
    # The consumer must also name WHICH producer to pull from.
    assert d_kv["remote_bootstrap_addr"] == "http://vllm-prefill:8998"
    assert d_kv["remote_engine_id"] == "engine-abc"


def test_colocated_handoff_sends_no_transfer_params() -> None:
    """src == dst has nothing to transfer; asking would have it pull from itself."""
    from gateway.queue import enqueue
    from gateway.types import Handoff

    class Spy:
        def __init__(self):
            self.id, self.bus, self.sent = "e0", KVBus(), []

        def enqueue(self, req, phase="both", kv_transfer_params=None):
            self.sent.append((phase, kv_transfer_params))
            return {"choices": []}

    worker = Spy()
    hop, _ = enqueue(Handoff(prefill=worker, decode=worker), _req())
    assert worker.sent == [("both", None)]
    assert hop is None


def test_the_worker_puts_transfer_params_on_the_wire() -> None:
    """Built correctly and then dropped before the POST is the same as absent."""
    import json

    from router.pools import VLLMWorker

    sent = {}

    class Wire(VLLMWorker):
        def _post(self, path, payload):
            sent.update(payload)
            return json.dumps({"choices": []})

    Wire("p0", "http://x:8000", model="m").enqueue(
        _req(), phase="prefill",
        kv_transfer_params={"transfer_id": "xfer-1", "do_remote_decode": True})

    assert sent["kv_transfer_params"]["transfer_id"] == "xfer-1"
    assert sent["max_tokens"] == 1, "the prefill leg must not generate"
    # The producer has to finish storing before the consumer asks.
    assert sent["stream"] is False


def test_bootstrap_url_is_the_worker_host_on_the_mooncake_port() -> None:
    from router.pools import _bootstrap_url

    assert _bootstrap_url("http://vllm-prefill:8000") == "http://vllm-prefill:8998"
    assert _bootstrap_url("http://10.0.0.5:8000/") == "http://10.0.0.5:8998"
    assert _bootstrap_url("") == ""


def test_concurrent_requests_do_not_share_a_transfer_id() -> None:
    """Each request needs its own id, because the KV handshake is keyed on it.

    queue.py builds transfer_id as f"xfer-{req.id}". request_from_openai used to
    default the id to the literal "http" whenever the payload omitted one — and
    every OpenAI client omits it, load.py included. So every concurrent request
    claimed the transfer id "xfer-http".

    The engine then receives a finished_recving for an id whose earlier owner has
    already completed and had its blocks freed, and this assert in vLLM's
    scheduler takes EngineCore down with it:

        assert req_id in self.requests

    Decode died four times this way during a capacity sweep, at which point the
    gateway shed every later request with 503 and the sweep measured nothing. It
    survived 1 and 2 sessions, which is what made it look like a capacity limit.
    """
    from gateway.types import request_from_openai
    from gateway.queue import kv_params

    payload = {"messages": [{"role": "user", "content": "hello"}]}
    ids = {request_from_openai(dict(payload)).id for _ in range(50)}
    assert len(ids) == 50, (
        f"only {len(ids)} distinct ids across 50 requests — concurrent requests "
        f"will collide on one transfer_id and kill the engine")
    assert not any(i == "http" for i in ids), "the old constant default is back"

    # And the handshake must carry that distinctness through to both legs.
    a, b = (request_from_openai(dict(payload)) for _ in range(2))
    assert (kv_params(f"xfer-{a.id}", producer=True)["transfer_id"]
            != kv_params(f"xfer-{b.id}", producer=True)["transfer_id"])


def test_an_explicit_id_is_still_honoured() -> None:
    """The harness and the behaviour tests drive specific ids on purpose."""
    from gateway.types import request_from_openai

    payload = {"messages": [{"role": "user", "content": "x"}], "id": "fixed-1"}
    assert request_from_openai(payload).id == "fixed-1"
