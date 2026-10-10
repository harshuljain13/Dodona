from __future__ import annotations

from router.kvbus import KVBus
from router.warmup import (
    WarmupPlan,
    apply,
    choose_plan,
    fast_plan,
    first_ttft_ms,
    naive_plan,
    report,
    warmup_ms,
)

def test_fast_plan_is_much_cheaper_than_capturing_every_batch() -> None:
    naive = naive_plan()
    fast = fast_plan()
    assert warmup_ms(fast) < 0.15 * warmup_ms(naive)
    assert fast.graph_buckets < naive.graph_buckets
    assert fast.compile_cache and fast.parallel_slices

def test_skipping_forward_shifts_cost_onto_first_ttft() -> None:
    cold = WarmupPlan(allocate=True, dummy_forward=False, graph_buckets=0)
    hot = WarmupPlan(allocate=True, dummy_forward=True, graph_buckets=2)
    assert warmup_ms(cold) < warmup_ms(hot)
    assert first_ttft_ms(cold) > first_ttft_ms(hot)

def test_compile_cache_cuts_forward_and_graphs_not_allocate() -> None:
    miss = WarmupPlan(graph_buckets=4, compile_cache=False, parallel_slices=True)
    hit = WarmupPlan(graph_buckets=4, compile_cache=True, parallel_slices=True)
    assert warmup_ms(hit) < warmup_ms(miss)
    no_alloc_hit = WarmupPlan(allocate=False, graph_buckets=0, dummy_forward=False)
    no_alloc_miss = WarmupPlan(allocate=False, graph_buckets=0, dummy_forward=False, compile_cache=True)
    assert warmup_ms(no_alloc_hit) == warmup_ms(no_alloc_miss) == 0

def test_parallel_slices_is_max_not_sum() -> None:
    serial = WarmupPlan(graph_buckets=2, parallel_slices=False, slices=2)
    parallel = WarmupPlan(graph_buckets=2, parallel_slices=True, slices=2)
    assert warmup_ms(serial) == 2 * warmup_ms(parallel)

def test_choose_plan_stays_inside_budget_and_keeps_allocate() -> None:
    plan = choose_plan(budget_ms=1500, prefix_tokens=256)
    assert warmup_ms(plan) <= 1500
    assert plan.allocate and plan.dummy_forward
    tight = choose_plan(budget_ms=1, prefix_tokens=256)
    assert tight.allocate and tight.dummy_forward
    assert warmup_ms(tight) >= 400

def test_apply_seeds_kvbus_so_first_prefix_is_a_hit() -> None:
    bus = KVBus()
    n = apply(bus, {"sys": 256, "tool": 64}, ["text-0", "vision-0"])
    assert n == 4
    assert bus.cached("text-0", "sys") == 256
    assert bus.cached("vision-0", "tool") == 64

def test_report_names_speedup() -> None:
    lines = "\n".join(report(fast_plan()))
    assert "warmup " in lines and "first_ttft " in lines
    assert "speedup " in lines
