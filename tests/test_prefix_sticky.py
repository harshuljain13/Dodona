from __future__ import annotations

import json
import random
from pathlib import Path

from router.router import PREFILL_SCORERS, Router
from router.kvbus import KVBus
from router.pools import FakeWorker
from gateway.types import Request, Shed

TRACES = Path(__file__).resolve().parents[1] / "traces"

def _load(name: str) -> list[Request]:
    out: list[Request] = []
    for line in (TRACES / name).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out.append(Request(**row))
    return out

def _replay(reqs: list[Request], n: int, policy: str, seed: int) -> list[FakeWorker]:
    bus = KVBus()
    workers = [FakeWorker(f"w{i}", bus=bus) for i in range(n)]
    epp = Router(workers, workers, policy=policy, rng=random.Random(seed))
    for i, req in enumerate(reqs):
        picked = epp.pick(workers, PREFILL_SCORERS, req, policy=policy)
        assert not isinstance(picked, Shed), f"shed at {i} policy={policy}"
        picked.enqueue(req, phase="prefill")
    return workers

def test_unique_prefix_p2c_max_load_gap_lt_random() -> None:
    reqs = _load("unique_prefix.jsonl")
    n = 16
    p2c_gaps = []
    rand_gaps = []
    for seed in range(5):
        p2c = _replay(reqs, n, "p2c", seed)
        rnd = _replay(reqs, n, "random", seed)
        p2c_loads = [w.kv_used for w in p2c]
        rnd_loads = [w.kv_used for w in rnd]
        p2c_gaps.append(max(p2c_loads) - min(p2c_loads))
        rand_gaps.append(max(rnd_loads) - min(rnd_loads))
    assert sum(p2c_gaps) < sum(rand_gaps)
    assert min(p2c_gaps) <= min(rand_gaps)

def test_shared_prefix_sticky_kv_lt_half_least_loaded() -> None:
    reqs = _load("shared_prefix.jsonl")
    n = 16
    sticky = _replay(reqs, n, "p2c", seed=0)
    ll = _replay(reqs, n, "least_loaded", seed=0)
    sticky_kv = sum(w.kv_used for w in sticky)
    ll_kv = sum(w.kv_used for w in ll)
    assert sticky_kv < 0.5 * ll_kv

def test_aborted_request_frees_kv_and_adds_no_decode_tokens() -> None:
    w = FakeWorker("w0")
    req = Request(
        id="abort-me",
        arrival_t=0.0,
        priority=1,
        prompt_tokens=256,
        max_new_tokens=64,
        prefix_hash="p",
        timeout_s=30.0,
        tenant="lab",
    )
    w.enqueue(req, phase="both")
    assert w.kv_used == 256
    assert w.decode_tokens_emitted == 0
    w.abort(req.id)
    assert w.kv_used == 0
    assert w.decode_tokens_emitted == 0
    w.step()
    w.run_until_idle()
    assert w.decode_tokens_emitted == 0
    assert req.aborted is True
