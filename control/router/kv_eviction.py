from __future__ import annotations

import argparse
from dataclasses import dataclass, field

@dataclass
class Prefix:
    prefix_hash: str
    tokens: int
    last_hit: float = 0.0
    hits: int = 1
    refcount: int = 1
    priority: int = 1
    worker_id: str = ""

@dataclass
class Victim:
    prefix_hash: str
    tokens: int
    worker_id: str
    reason: str

@dataclass
class KVCache:
    capacity_tokens: int
    block_size: int = 16
    entries: dict[tuple[str, str], Prefix] = field(default_factory=dict)

    def _key(self, prefix_hash: str, worker_id: str) -> tuple[str, str]:
        return (worker_id, prefix_hash)

    def used(self) -> int:
        return sum(p.tokens for p in self.entries.values())

    def free(self) -> int:
        return max(0, self.capacity_tokens - self.used())

    def blocks(self, tokens: int) -> int:
        return (int(tokens) + self.block_size - 1) // self.block_size

    def put(
        self,
        prefix_hash: str,
        tokens: int,
        *,
        now: float = 0.0,
        hits: int = 1,
        refcount: int = 1,
        priority: int = 1,
        worker_id: str = "",
    ) -> Prefix:
        key = self._key(prefix_hash, worker_id)
        entry = Prefix(
            prefix_hash=prefix_hash,
            tokens=int(tokens),
            last_hit=float(now),
            hits=int(hits),
            refcount=int(refcount),
            priority=int(priority),
            worker_id=worker_id,
        )
        self.entries[key] = entry
        return entry

    def hit(self, prefix_hash: str, *, now: float, worker_id: str = "") -> int:
        key = self._key(prefix_hash, worker_id)
        entry = self.entries.get(key)
        if entry is None:
            return 0
        entry.last_hit = float(now)
        entry.hits += 1
        return entry.tokens

    def drop(self, prefix_hash: str, worker_id: str = "") -> int:
        key = self._key(prefix_hash, worker_id)
        entry = self.entries.pop(key, None)
        return 0 if entry is None else entry.tokens

    def rank(self, policy: str) -> list[Prefix]:
        rows = list(self.entries.values())
        if policy == "lru":
            return sorted(rows, key=lambda p: (p.last_hit, p.prefix_hash))
        if policy == "lfu":
            return sorted(rows, key=lambda p: (p.hits, p.last_hit, p.prefix_hash))
        if policy == "priority":
            return sorted(rows, key=lambda p: (p.priority, p.last_hit, p.prefix_hash))
        if policy == "prefix_protect":
            return sorted(
                rows,
                key=lambda p: (p.refcount > 1, p.last_hit, p.prefix_hash),
            )
        raise ValueError(f"unknown policy {policy}")

    def evict(self, need_tokens: int, policy: str = "prefix_protect") -> list[Victim]:
        need = max(0, int(need_tokens) - self.free())
        if need <= 0:
            return []
        victims: list[Victim] = []
        freed = 0
        for entry in self.rank(policy):
            if freed >= need:
                break
            self.drop(entry.prefix_hash, entry.worker_id)
            victims.append(
                Victim(
                    prefix_hash=entry.prefix_hash,
                    tokens=entry.tokens,
                    worker_id=entry.worker_id,
                    reason=policy,
                )
            )
            freed += entry.tokens
        return victims

def from_bus(bus: object, *, capacity_tokens: int, now: float = 0.0) -> KVCache:
    cache = KVCache(capacity_tokens=int(capacity_tokens))
    cached = getattr(bus, "_cached", {})
    for i, ((worker_id, prefix_hash), tokens) in enumerate(cached.items()):
        cache.put(
            str(prefix_hash),
            int(tokens),
            now=float(now) + i,
            worker_id=str(worker_id),
        )
    return cache

def apply(bus: object, victims: list[Victim]) -> int:
    evict = getattr(bus, "evict", None)
    if evict is None:
        return 0
    return sum(int(evict(victim.worker_id, victim.prefix_hash)) for victim in victims)

def report(cache: KVCache, victims: list[Victim], policy: str) -> list[str]:
    lines = [
        f"kvcache used={cache.used()} free={cache.free()} "
        f"cap={cache.capacity_tokens} policy={policy} victims={len(victims)}"
    ]
    for victim in victims:
        lines.append(
            f"  drop {victim.worker_id or '-'} {victim.prefix_hash} "
            f"tokens={victim.tokens} reason={victim.reason}"
        )
    return lines

def demo(policy: str, need: int) -> list[str]:
    cache = KVCache(capacity_tokens=4096)
    cache.put("shared", 2048, now=1.0, hits=8, refcount=3, priority=2, worker_id="text-0")
    cache.put("old-unique", 1024, now=0.0, hits=1, refcount=1, priority=1, worker_id="text-0")
    cache.put("new-unique", 1024, now=2.0, hits=2, refcount=1, priority=1, worker_id="text-0")
    victims = cache.evict(need, policy=policy)
    return report(cache, victims, policy)

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Class 10 KV cache eviction")
    p.add_argument("--policy", default="prefix_protect", choices=("lru", "lfu", "priority", "prefix_protect"))
    p.add_argument("--need", type=int, default=1024)
    args = p.parse_args(argv)
    for line in demo(args.policy, args.need):
        print(line)

if __name__ == "__main__":
    main()
