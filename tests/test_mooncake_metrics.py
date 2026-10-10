from __future__ import annotations

from router.mooncake import Store

def test_mooncake_metrics_count_hops_and_tokens() -> None:
    store = Store()
    store.put({"req_id": "a", "src": "p0", "dst": "d0", "tokens": 16})
    store.put({"req_id": "b", "src": "p0", "dst": "d0", "tokens": 8})
    text = store.render_metrics()
    assert "mooncake_hops_total 2" in text
    assert "mooncake_blocks 2" in text
    assert "mooncake_hop_tokens_total 24" in text
