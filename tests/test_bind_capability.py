from __future__ import annotations

from router.router import Router, infer_capability
from gateway.admission import Gateway
from router.kvbus import KVBus
from gateway.metrics import METRICS
from router.pools import FakeWorker
from gateway.types import Request, Snapshot, request_messages

def _req(**kw) -> Request:
    base = dict(
        id="cap",
        arrival_t=0.0,
        priority=1,
        prompt_tokens=16,
        max_new_tokens=4,
        prefix_hash="h",
        timeout_s=30.0,
        tenant="lab",
        capability="text",
    )
    base.update(kw)
    return Request(**base)

def test_infer_capability_from_model_and_image() -> None:
    assert infer_capability({"model": "Qwen/Qwen2.5-3B-Instruct"}) == "text"
    assert infer_capability({"model": "Qwen/Qwen2.5-VL-3B-Instruct"}) == "vision"
    assert infer_capability(
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]}
    ) == "vision"

def test_capability_bind_stays_on_one_model_no_kv_hop(monkeypatch) -> None:
    monkeypatch.setenv("LAB_SPLIT", "capability")
    METRICS.reset()
    bus = KVBus()
    text = FakeWorker("text-0", bus=bus)
    vision = FakeWorker("vision-0", bus=bus)
    gw = Gateway(Router([text], [vision]))
    text_resp = gw.handle(_req(id="t", capability="text"))
    vision_resp = gw.handle(_req(id="v", capability="vision"))
    assert text_resp.status == 200 and vision_resp.status == 200
    assert text_resp.body.get("kv_hop") is None
    assert vision_resp.body.get("kv_hop") is None
    assert "t" in text._reqs and "t" not in vision._reqs
    assert "v" in vision._reqs and "v" not in text._reqs
    assert METRICS.kv_transfer_total == 0

def test_unknown_capability_sheds_without_enqueue(monkeypatch) -> None:
    monkeypatch.setenv("LAB_SPLIT", "capability")
    bus = KVBus()
    text = FakeWorker("text-0", bus=bus)
    vision = FakeWorker("vision-0", bus=bus)
    gw = Gateway(Router([text], [vision]))
    resp = gw.handle(_req(capability="audio"))
    assert resp.status == 503
    assert not text._reqs and not vision._reqs

def test_request_messages_prefer_prompt() -> None:
    req = _req()
    req.prompt = "hello gpu"
    assert request_messages(req) == [{"role": "user", "content": "hello gpu"}]

def test_gateway_puts_model_text_on_the_body(monkeypatch) -> None:
    monkeypatch.setenv("LAB_SPLIT", "capability")

    class Toy:
        id = "text-0"

        def snapshot(self) -> Snapshot:
            return Snapshot(
                pod_id=self.id,
                age_s=0.0,
                healthy=True,
                kv_free_ratio=0.9,
                tokens_in_flight=0,
                prefix_tokens={},
            )

        def enqueue(self, req: Request, phase: str = "both") -> dict:
            return {
                "id": "cmpl-1",
                "model": "toy",
                "choices": [{"message": {"content": "hello from the gpu"}}],
            }

    gw = Gateway(Router([Toy()], [FakeWorker("vision-0")]))
    resp = gw.handle(_req(capability="text"))
    assert resp.status == 200
    assert resp.body["model"] == "toy"
    assert resp.body["choices"][0]["message"]["content"] == "hello from the gpu"

def test_unreachable_worker_is_503(monkeypatch) -> None:
    monkeypatch.setenv("LAB_SPLIT", "capability")
    text = FakeWorker("text-0")

    def boom(req: Request, phase: str = "both"):
        raise OSError("connection refused")

    text.enqueue = boom
    gw = Gateway(Router([text], [FakeWorker("vision-0")]))
    resp = gw.handle(_req(capability="text"))
    assert resp.status == 503
    assert resp.error == "server_is_overloaded"
