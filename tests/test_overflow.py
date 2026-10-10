from __future__ import annotations

import json
from pathlib import Path

from router.overflow import FakeOverflow, send
from router.router import Router
from gateway.admission import Gateway
from router.kvbus import KVBus
from gateway.metrics import METRICS
from router.pools import FakeWorker
from router.trace import TRACES
from gateway.types import Request

def _req(**kw) -> Request:
    base = dict(
        id="ov",
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

def test_429_does_not_call_backup_or_router() -> None:
    METRICS.reset()
    bus = KVBus()
    router = Router([FakeWorker("p0", bus=bus)], [FakeWorker("d0", bus=bus)])
    called = {"n": 0}
    orig = router.place

    def wrapped(req: Request):
        called["n"] += 1
        return orig(req)

    router.place = wrapped
    gw = Gateway(router, tokens_per_min=10)
    port = FakeOverflow()
    resp = send(_req(prompt_tokens=64, max_new_tokens=8), gw, port)
    assert resp.status == 429
    assert resp.via == "local"
    assert called["n"] == 0
    assert port.calls == []
    assert TRACES.overflow_count() == 0
    assert TRACES.events[-1]["via"] == "local"
    assert TRACES.events[-1]["happened"] == "tenant_refuse"
    assert "local_ms" in TRACES.events[-1]

def test_503_backup_called_once_no_cross_pool_hop() -> None:
    METRICS.reset()
    bus = KVBus()
    prefill = [FakeWorker("p0", bus=bus)]
    decode = [FakeWorker("d0", bus=bus)]
    for w in prefill + decode:
        w.saturating = True
    gw = Gateway(Router(prefill, decode))
    port = FakeOverflow()
    req = _req()
    resp = send(req, gw, port)
    assert resp.status == 200
    assert resp.via == "overflow"
    assert resp.local_status == 503
    assert resp.headers["X-Via"] == "overflow"
    assert resp.body["via"] == "overflow"
    assert port.calls == [req]
    assert all(not w._reqs for w in prefill + decode)
    assert TRACES.overflow_count() == 1
    ev = TRACES.events[-1]
    assert ev["via"] == "overflow"
    assert ev["local_status"] == 503
    assert ev["happened"] == "overflow_ok"
    assert "local_ms" in ev and "overflow_ms" in ev and "total_ms" in ev
    assert ev["total_ms"] >= ev["local_ms"]

def test_trace_path_labels_overflow_after_local_503(tmp_path, monkeypatch) -> None:
    dest = tmp_path / "requests.jsonl"
    monkeypatch.setenv("TRACE_PATH", str(dest))
    METRICS.reset()
    bus = KVBus()
    prefill = [FakeWorker("p0", bus=bus)]
    decode = [FakeWorker("d0", bus=bus)]
    for w in prefill + decode:
        w.saturating = True
    gw = Gateway(Router(prefill, decode))
    resp = send(_req(), gw, FakeOverflow())
    assert resp.via == "overflow"
    rows = [json.loads(ln) for ln in dest.read_text().splitlines() if ln.strip()]
    assert rows[-1]["via"] == "overflow"
    assert rows[-1]["local_status"] == 503
    assert rows[-1]["happened"] == "overflow_ok"
    assert "overflow_ms" in rows[-1]
    assert "total_ms" in rows[-1]

def test_overflow_source_does_not_import_place_or_pools() -> None:
    root = Path(__file__).resolve().parents[1]
    src = (root / "router" / "overflow.py").read_text()
    assert "from router.router" not in src and "import router.router" not in src
    assert "from gateway.admission" not in src and "import gateway.admission" not in src
    assert "from router.pools" not in src and "import router.pools" not in src
    assert "from gateway.queue" not in src and "import gateway.queue" not in src
    assert "provider" not in src
    assert "superlinked" not in src.lower()
    assert "mooncake" not in src.lower()
    for rel in (
        ("gateway", "admission.py"),
        ("gateway", "queue.py"),
        ("router", "router.py"),
        ("router", "planner.py"),
    ):
        other = root.joinpath(*rel).read_text()
        assert "router.overflow" not in other
        assert "from router.overflow" not in other
