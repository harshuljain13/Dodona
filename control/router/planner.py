from __future__ import annotations

from gateway.metrics import METRICS
from gateway.types import PLAN_HIGH_WATER

def plan(
    *,
    current_prefill: int,
    current_decode: int,
    uncached_prefill_tokens_in_flight: int,
    prefill_token_budget: int,
    decode_slots_used: int,
    decode_slots: int,
) -> tuple[int, int]:

    desired_prefill = current_prefill
    desired_decode = current_decode
    notes: list[str] = []

    if prefill_token_budget > 0:
        if uncached_prefill_tokens_in_flight > PLAN_HIGH_WATER * prefill_token_budget:
            desired_prefill += 1
            notes.append("scale prefill +1")
    if decode_slots > 0:
        if decode_slots_used > PLAN_HIGH_WATER * decode_slots:
            desired_decode += 1
            notes.append("scale decode +1")

    METRICS.set_desired("prefill", desired_prefill)
    METRICS.set_desired("decode", desired_decode)
    print(f"desired_replicas prefill={desired_prefill} decode={desired_decode}")
    for note in notes:
        print(note)
    return desired_prefill, desired_decode
