from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from gateway.metrics import METRICS
from gateway.types import Request

class KVBus:

    def __init__(self) -> None:
        self._cached: dict[tuple[str, str], int] = {}
        self.hops: list[dict] = []

    def cached(self, worker_id: str, prefix_hash: str | None) -> int:
        if not prefix_hash:
            return 0
        return self._cached.get((worker_id, prefix_hash), 0)

    def record(self, worker_id: str, prefix_hash: str | None, tokens: int) -> None:
        if not prefix_hash:
            return
        key = (worker_id, prefix_hash)
        self._cached[key] = max(self._cached.get(key, 0), tokens)

    def forget_worker(self, worker_id: str) -> None:
        self._cached = {k: v for k, v in self._cached.items() if k[0] != worker_id}

    def prefixes_for(self, worker_id: str) -> dict[str, int]:
        return {h: n for (wid, h), n in self._cached.items() if wid == worker_id}

    def evict(self, worker_id: str, prefix_hash: str | None = None) -> int:

        if prefix_hash:
            key = (worker_id, prefix_hash)
            n = 1 if key in self._cached else 0
            self._cached.pop(key, None)
        else:
            keys = [k for k in self._cached if k[0] == worker_id]
            n = len(keys)
            for key in keys:
                del self._cached[key]
        if n:
            METRICS.inc_kv_evict(n)
        return n

    def evict_workers(self, worker_ids: list[str], prefix_hash: str | None = None) -> int:
        return sum(self.evict(wid, prefix_hash) for wid in worker_ids)

    def transfer(self, src: str, dst: str, req: Request) -> dict:

        tokens = self.cached(src, req.prefix_hash) or int(req.prompt_tokens)
        if req.prefix_hash:
            self.record(dst, req.prefix_hash, tokens)
        hop = {
            "src": src,
            "dst": dst,
            "req_id": req.id,
            "tokens": tokens,
            "backend": os.environ.get("KV_BACKEND", "mooncake"),
            "prefix": req.prefix_hash,
        }
        self.hops.append(hop)
        METRICS.inc_kv_transfer(tokens)
        _backend_put(hop)
        return hop

def _backend_put(hop: dict) -> None:
    backend = hop.get("backend") or "mooncake"
    if backend == "nccl":
        from router.nccl import put
        put(hop)
        return
    if backend == "nixl":
        from router.nixl import put
        put(hop)
        return
    _store_put(hop)

def _store_put(hop: dict) -> None:
    base = os.environ.get("MOONCAKE_URL", "").strip().rstrip("/")
    if not base:
        return
    data = json.dumps(hop).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/put",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except (urllib.error.URLError, TimeoutError, OSError):
        return
