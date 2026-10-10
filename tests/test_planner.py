from __future__ import annotations

from router.planner import plan

def test_planner_prints_scale_prefill_plus_one_across_threshold(capsys) -> None:
    below = plan(
        current_prefill=1,
        current_decode=1,
        uncached_prefill_tokens_in_flight=70,
        prefill_token_budget=100,
        decode_slots_used=10,
        decode_slots=100,
    )
    out_below = capsys.readouterr().out
    assert below == (1, 1)
    assert "desired_replicas prefill=1 decode=1" in out_below
    assert "scale prefill +1" not in out_below

    above = plan(
        current_prefill=1,
        current_decode=1,
        uncached_prefill_tokens_in_flight=71,
        prefill_token_budget=100,
        decode_slots_used=10,
        decode_slots=100,
    )
    out_above = capsys.readouterr().out
    assert above == (2, 1)
    assert "desired_replicas prefill=2 decode=1" in out_above
    assert "scale prefill +1" in out_above
    assert "scale decode +1" not in out_above
