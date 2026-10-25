from __future__ import annotations

import pytest




def test_the_engine_gets_only_its_slice_of_the_speech_budget() -> None:
    """1.5s is the whole speech path, not the engine's share.

    ASR, the engine and TTS are serial on the way to first audio, so a TTFT
    target of 1.5s spends the entire budget on first-token and leaves nothing
    for the rest. 03-capacity derives the cluster's concurrency limit from this
    number, so getting it wrong overstates capacity.
    """
    from slos import SLOS, SPEECH_BUDGET_S, SPEECH_STAGES, TTFT_BUDGET_S

    assert TTFT_BUDGET_S == pytest.approx(SPEECH_BUDGET_S - sum(SPEECH_STAGES.values()))
    assert SLOS["ttft_p95"].target == TTFT_BUDGET_S, (
        "the SLO must be the derived slice, not a separate hardcoded number")
    assert 0 < TTFT_BUDGET_S < SPEECH_BUDGET_S


def test_itl_target_stays_under_the_stutter_floor() -> None:
    """Slower than one token per 300ms and TTS runs dry mid-sentence.

    Speech is consumed at ~3.3 tokens/s. An ITL above that floor means the
    synthesiser catches up with the generator and the listener hears a gap, so
    the target has to sit meaningfully below it rather than at it.
    """
    from slos import ITL_BUDGET_S, ITL_STUTTER_S, SLOS, SPEECH_TOKENS_PER_S

    assert ITL_STUTTER_S == pytest.approx(1.0 / SPEECH_TOKENS_PER_S, abs=1e-3)
    assert ITL_BUDGET_S < ITL_STUTTER_S / 2, (
        f"ITL target {ITL_BUDGET_S}s leaves too little buffer under the "
        f"{ITL_STUTTER_S}s stutter floor")
    assert SLOS["itl_p95"].target == ITL_BUDGET_S


def test_there_is_exactly_one_slo_set() -> None:
    """No per-workload SLO classes.

    I proposed splitting the promise into "conversational" and "research"
    classes so that a measured 2.4s TTFT would pass one of them. That is moving
    the goalpost to meet the measurement: the SLO is a constant, capacity is
    derived from it, and admission control keeps offered load under that
    capacity. A promise relaxed because the system missed it makes every later
    comparison a tautology.
    """
    from slos import SLOS

    for key in SLOS:
        for suffix in ("_conversational", "_research", "_light", "_heavy"):
            assert suffix not in key, (
                f"{key} is a per-workload SLO variant — there is one promise, "
                f"and load is what varies against it")
