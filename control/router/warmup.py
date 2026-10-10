from __future__ import annotations

import argparse
from dataclasses import dataclass

from router.kvbus import KVBus

ALLOCATE_MS = 400
FORWARD_MS = 800
GRAPH_MS = 250
PREFIX_MS_PER_TOKEN = 0.05
CACHE_HIT = 0.15

@dataclass(frozen=True)
class WarmupPlan:
    allocate: bool = True
    dummy_forward: bool = True
    graph_buckets: int = 8
    prefix_tokens: int = 0
    compile_cache: bool = False
    parallel_slices: bool = False
    slices: int = 2

def _compile_ms(plan: WarmupPlan) -> float:
    graphs = max(0, int(plan.graph_buckets)) * GRAPH_MS
    compile_part = (FORWARD_MS if plan.dummy_forward else 0) + graphs
    if plan.compile_cache:
        compile_part *= CACHE_HIT
    return compile_part

def warmup_ms(plan: WarmupPlan) -> int:
    allocate = ALLOCATE_MS if plan.allocate else 0
    prefix = plan.prefix_tokens * PREFIX_MS_PER_TOKEN
    per_slice = allocate + _compile_ms(plan) + prefix
    if plan.parallel_slices:
        return int(round(per_slice))
    return int(round(per_slice * max(1, plan.slices)))

def leftover_ms(plan: WarmupPlan) -> int:
    skipped = WarmupPlan(
        allocate=not plan.allocate,
        dummy_forward=not plan.dummy_forward,
        graph_buckets=0 if plan.graph_buckets else 8,
        prefix_tokens=0,
        compile_cache=False,
        parallel_slices=plan.parallel_slices,
        slices=plan.slices,
    )
    if plan.allocate and plan.dummy_forward:
        skipped = WarmupPlan(
            allocate=False,
            dummy_forward=False,
            graph_buckets=max(0, 8 - plan.graph_buckets),
            prefix_tokens=0,
            compile_cache=False,
            parallel_slices=plan.parallel_slices,
            slices=plan.slices,
        )
    return warmup_ms(skipped) if (not plan.allocate or not plan.dummy_forward or plan.graph_buckets < 8) else 0

def first_ttft_ms(plan: WarmupPlan, request_ms: int = 40) -> int:
    return leftover_ms(plan) + int(request_ms)

def choose_plan(*, budget_ms: int, prefix_tokens: int = 256) -> WarmupPlan:
    candidates = [
        WarmupPlan(graph_buckets=2, prefix_tokens=prefix_tokens, compile_cache=True, parallel_slices=True),
        WarmupPlan(graph_buckets=1, prefix_tokens=prefix_tokens, compile_cache=True, parallel_slices=True),
        WarmupPlan(graph_buckets=1, prefix_tokens=prefix_tokens, compile_cache=True, parallel_slices=False),
    ]
    fits = [p for p in candidates if warmup_ms(p) <= budget_ms]
    if not fits:
        return WarmupPlan(graph_buckets=1, compile_cache=True, parallel_slices=True, prefix_tokens=0)
    return min(fits, key=warmup_ms)

def naive_plan(*, slices: int = 2) -> WarmupPlan:
    return WarmupPlan(graph_buckets=32, prefix_tokens=0, compile_cache=False, parallel_slices=False, slices=slices)

def fast_plan(*, prefix_tokens: int = 256, slices: int = 2) -> WarmupPlan:
    return WarmupPlan(
        graph_buckets=2,
        prefix_tokens=prefix_tokens,
        compile_cache=True,
        parallel_slices=True,
        slices=slices,
    )

def apply(bus: KVBus, prefixes: dict[str, int], worker_ids: list[str]) -> int:
    n = 0
    for worker_id in worker_ids:
        for prefix_hash, tokens in prefixes.items():
            bus.record(worker_id, prefix_hash, int(tokens))
            n += 1
    return n

def report(plan: WarmupPlan, *, request_ms: int = 40) -> list[str]:
    naive = naive_plan(slices=plan.slices)
    return [
        f"warmup {warmup_ms(plan)}ms  first_ttft {first_ttft_ms(plan, request_ms)}ms  "
        f"graphs={plan.graph_buckets} cache={int(plan.compile_cache)} "
        f"parallel={int(plan.parallel_slices)} prefix={plan.prefix_tokens}",
        f"naive {warmup_ms(naive)}ms  first_ttft {first_ttft_ms(naive, request_ms)}ms  "
        f"speedup {warmup_ms(naive) / warmup_ms(plan):.2f}x",
    ]

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Class 10 replica warmup")
    p.add_argument("--budget", type=int, default=1500)
    p.add_argument("--prefix", type=int, default=256)
    p.add_argument("--naive", action="store_true")
    args = p.parse_args(argv)
    plan = naive_plan() if args.naive else choose_plan(budget_ms=args.budget, prefix_tokens=args.prefix)
    for line in report(plan):
        print(line)

if __name__ == "__main__":
    main()
