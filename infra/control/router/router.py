from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Sequence

from gateway.metrics import METRICS
from router.planner import plan as plan_replicas
from gateway.types import (
    KV_SATURATION,
    STALE_S,
    STICKY_OVERLAP,
    STICKY_TAU,
    Handoff,
    Request,
    Shed,
    Snapshot,
    Worker,
)

Scorer = Callable[[Snapshot, Request], float | None]

TOP_K = 4

def score_prefix_cache(snap: Snapshot, req: Request) -> float | None:
    if snap.prefix_tokens is None:
        return None
    if not req.prefix_hash or req.prompt_tokens <= 0:
        return 0.0
    hit = snap.prefix_tokens.get(req.prefix_hash, 0)
    return hit / req.prompt_tokens

def score_token_load(snap: Snapshot, req: Request) -> float | None:
    if snap.tokens_in_flight is None:
        return None
    return -float(snap.tokens_in_flight)

def score_active_request(snap: Snapshot, req: Request) -> float | None:
    if snap.active_requests is None:
        return None
    return -float(snap.active_requests)

def score_kv_utilisation(snap: Snapshot, req: Request) -> float | None:
    if snap.kv_free_ratio is None:
        return None
    return float(snap.kv_free_ratio)

def score_queue_depth(snap: Snapshot, req: Request) -> float | None:
    if snap.queue_depth is None:
        return None
    return -float(snap.queue_depth)

WEIGHTS: dict[Scorer, float] = {
    score_prefix_cache: 2.0,
    score_token_load: 1.0,
    score_active_request: 1.0,
    score_kv_utilisation: 1.0,
    score_queue_depth: 1.0,
}

PREFILL_SCORERS: list[Scorer] = [score_prefix_cache, score_token_load, score_kv_utilisation]
DECODE_SCORERS: list[Scorer] = [score_active_request, score_kv_utilisation]

def is_unknown(snap: Snapshot | None) -> bool:
    if snap is None:
        return True
    return snap.age_s > STALE_S

def is_saturating(snap: Snapshot) -> bool:
    if snap.saturating:
        return True
    if snap.kv_free_ratio is not None and snap.kv_free_ratio < (1.0 - KV_SATURATION):
        return True
    return False

def infer_capability(payload: dict) -> str:

    raw = payload.get("capability")
    if raw:
        return str(raw).lower()
    model = str(payload.get("model") or "").lower()
    if any(tag in model for tag in ("-vl", "vision", "llava", "pixtral", "qwen2-vl")):
        return "vision"
    for msg in payload.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                return "vision"
    return "text"

def _shed_fleet(reason: str = "no_eligible_pod") -> Shed:
    return Shed(
        status=503,
        error="server_is_overloaded",
        reason=reason,
        retry_after_s=2,
        message="fleet saturating",
    )

class Router:

    def __init__(
        self,
        prefill: Sequence[Worker],
        decode: Sequence[Worker],
        *,
        policy: str = "p2c",
        tau: int = STICKY_TAU,
        rng: random.Random | None = None,
    ) -> None:
        self.prefill = list(prefill)
        self.decode = list(decode)
        self.policy = policy
        self.tau = tau
        self.rng = rng or random.Random()
        self.last_decode_scorers: list[Scorer] = []
        self.last_prefill_scorers: list[Scorer] = []

    plan = staticmethod(plan_replicas)

    def place(self, req: Request) -> Handoff | Shed:

        if not req.force_phase and os.environ.get("LAB_SPLIT", "phase") == "capability":
            return self._place_capability(req)
        p = self.pick(self.prefill, PREFILL_SCORERS, req)
        if isinstance(p, Shed):
            return p
        d = self.pick(self.decode, DECODE_SCORERS, req)
        if isinstance(d, Shed):
            return d
        return Handoff(prefill=p, decode=d)

    def _place_capability(self, req: Request) -> Handoff | Shed:

        cap = (req.capability or "text").lower()
        if cap == "vision":
            picked = self.pick(self.decode, DECODE_SCORERS, req)
        elif cap == "text":
            picked = self.pick(self.prefill, PREFILL_SCORERS, req)
        else:
            return _shed_fleet("no_eligible_pod")
        if isinstance(picked, Shed):
            return picked
        return Handoff(prefill=picked, decode=picked)

    def pick(
        self,
        pool: Sequence[Worker],
        scorers: Sequence[Scorer],
        req: Request,
        *,
        policy: str | None = None,
    ) -> Worker | Shed:
        if scorers == DECODE_SCORERS or (score_prefix_cache not in scorers and score_active_request in scorers):
            self.last_decode_scorers = list(scorers)
        if score_prefix_cache in scorers:
            self.last_prefill_scorers = list(scorers)

        t0 = time.perf_counter()
        eligible = self._filter(pool)
        if not eligible:
            METRICS.observe_duration("pick", time.perf_counter() - t0)
            return _shed_fleet(self._shed_reason(pool))
        under_deadline = [
            (w, s) for w, s in eligible if self._queue_wait_s(s) <= req.timeout_s / 2
        ]
        if eligible and not under_deadline:
            METRICS.observe_duration("pick", time.perf_counter() - t0)
            return _shed_fleet("timeout_queue")
        if under_deadline:
            eligible = under_deadline

        scored = [(self._score(snap, req, scorers), worker) for worker, snap in eligible]
        mode = policy or self.policy

        if mode not in ("least_loaded", "random") and score_prefix_cache in scorers:
            stuck = self._sticky(eligible, req)
            if stuck is not None:
                METRICS.inc_pick()
                METRICS.inc_sticky()
                METRICS.observe_duration("pick", time.perf_counter() - t0)
                return stuck
        if mode == "random":
            chosen = self.rng.choice([w for _, w in scored])
        elif mode == "least_loaded":
            chosen = min(eligible, key=lambda ws: self._load_key(ws[1]))[0]
        elif mode == "max" or len(scored) == 1:
            chosen = max(scored, key=lambda t: t[0])[1]
        else:
            ranked = sorted(scored, key=lambda t: t[0], reverse=True)
            top = ranked[: max(TOP_K, 2)]
            if len(top) >= 2:
                a, b = self.rng.sample(top, 2)
                chosen = a[1] if a[0] >= b[0] else b[1]
            else:
                chosen = top[0][1]
        METRICS.inc_pick()
        METRICS.observe_duration("pick", time.perf_counter() - t0)
        return chosen

    def _filter(self, pool: Sequence[Worker]) -> list[tuple[Worker, Snapshot]]:
        snaps: list[tuple[Worker, Snapshot | None]] = []
        for w in pool:
            snaps.append((w, w.snapshot()))

        fresh: list[tuple[Worker, Snapshot]] = []
        saw_unknown = False
        for w, s in snaps:
            if is_unknown(s) or s is None:
                saw_unknown = True
                continue
            if not s.healthy:
                continue
            if is_saturating(s):
                continue
            fresh.append((w, s))
        if fresh:
            if saw_unknown:
                METRICS.inc_unknown_snapshot()
            return fresh

        if not snaps or not all(is_unknown(s) for _, s in snaps):
            return []
        METRICS.inc_unknown_snapshot()
        allowed: list[tuple[Worker, Snapshot]] = []
        for w, s in snaps:
            if s is None or not s.healthy:
                continue
            if is_saturating(s):
                continue
            allowed.append((w, s))
        return allowed

    def _sticky(self, eligible: Sequence[tuple[Worker, Snapshot]], req: Request) -> Worker | None:
        if not req.prefix_hash or req.prompt_tokens <= 0:
            return None
        for worker, snap in eligible:
            if snap.prefix_tokens is None:
                continue
            overlap = snap.prefix_tokens.get(req.prefix_hash, 0)
            if overlap / req.prompt_tokens < STICKY_OVERLAP:
                continue
            in_flight = snap.tokens_in_flight
            if in_flight is None:
                continue
            if in_flight < self.tau:
                return worker
        return None

    def _score(self, snap: Snapshot, req: Request, scorers: Sequence[Scorer]) -> float:
        total = 0.0
        weight = 0.0
        for fn in scorers:
            value = fn(snap, req)
            if value is None:
                continue
            w = WEIGHTS.get(fn, 1.0)
            total += w * value
            weight += w
        if weight == 0.0:

            return float("-inf")
        return total

    @staticmethod
    def _load_key(snap: Snapshot) -> tuple[int, int]:
        load = snap.tokens_in_flight
        if load is None:
            return (1, 10**12)
        return (0, load)

    @staticmethod
    def _queue_wait_s(snap: Snapshot) -> float:
        depth = snap.queue_depth
        if depth is None:
            return 0.0
        return float(depth) * 0.2

    @staticmethod
    def _shed_reason(pool: Sequence[Worker]) -> str:
        for w in pool:
            snap = w.snapshot()
            if snap.kv_free_ratio is not None and snap.kv_free_ratio < (1.0 - KV_SATURATION):
                return "kv_free"
        return "no_eligible_pod"

plan = plan_replicas
