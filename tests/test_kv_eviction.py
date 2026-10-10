from __future__ import annotations

from router.kv_eviction import KVCache, Prefix, Victim, apply, from_bus
from router.kvbus import KVBus
from gateway.metrics import METRICS

def _cache() -> KVCache:
    cache = KVCache(capacity_tokens=4096)
    cache.put("shared", 2048, now=1.0, hits=8, refcount=3, priority=2, worker_id="w0")
    cache.put("old-unique", 1024, now=0.0, hits=1, refcount=1, priority=1, worker_id="w0")
    cache.put("new-unique", 1024, now=2.0, hits=2, refcount=1, priority=0, worker_id="w0")
    return cache

def test_prefix_protect_drops_unique_before_shared() -> None:
    cache = _cache()
    victims = cache.evict(1024, policy="prefix_protect")
    assert [v.prefix_hash for v in victims] == ["old-unique"]
    assert "shared" in {p.prefix_hash for p in cache.entries.values()}
    assert cache.free() >= 1024

def test_lru_drops_oldest() -> None:
    cache = _cache()
    victims = cache.evict(1024, policy="lru")
    assert [v.prefix_hash for v in victims] == ["old-unique"]

def test_lfu_drops_coldest() -> None:
    cache = _cache()
    victims = cache.evict(1024, policy="lfu")
    assert [v.prefix_hash for v in victims] == ["old-unique"]

def test_priority_drops_lowest_tenant() -> None:
    cache = _cache()
    victims = cache.evict(1024, policy="priority")
    assert [v.prefix_hash for v in victims] == ["new-unique"]

def test_evict_stops_once_need_is_met() -> None:
    cache = _cache()
    victims = cache.evict(1024, policy="prefix_protect")
    assert len(victims) == 1
    assert sum(v.tokens for v in victims) >= 1024

def test_hit_keeps_prefix_off_the_lru_chopping_block() -> None:
    cache = _cache()
    assert cache.hit("old-unique", now=9.0, worker_id="w0") == 1024
    victims = cache.evict(1024, policy="lru")
    assert [v.prefix_hash for v in victims] == ["shared"]

def test_from_bus_and_apply_drop_kvbus_prefix() -> None:
    bus = KVBus()
    bus.record("w0", "old-unique", 1024)
    bus.record("w0", "shared", 2048)
    cache = from_bus(bus, capacity_tokens=2048)
    assert cache.used() == 3072
    victims = cache.evict(2048, policy="lru")
    dropped = apply(bus, victims)
    assert dropped >= 1
    assert bus.cached("w0", "old-unique") == 0
    assert METRICS.kv_evict_total >= 1

def test_unknown_policy_raises() -> None:
    cache = KVCache(capacity_tokens=16)
    cache.put("p", 8, worker_id="w0")
    try:
        cache.rank("random")
    except ValueError as exc:
        assert "unknown policy" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert isinstance(Prefix("p", 8), Prefix)
    assert Victim("p", 8, "w0", "lru").reason == "lru"
