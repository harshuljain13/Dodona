from __future__ import annotations

import pytest




def test_the_engine_gets_only_its_slice_of_the_speech_budget() -> None:
    """1.5s is the whole speech path, not the engine's share.

    ASR, retrieval, the engine and TTS are serial on the way to first audio, so
    an ttft_p95 target of 1.5s spends the entire conversational budget on
    first-token and leaves nothing for the other three stages. 03-capacity
    derives the cluster's concurrency limit from this number, so getting it
    wrong overstates capacity.
    """
    from slos import SLOS, SPEECH_BUDGET_S, SPEECH_STAGES, TTFT_BUDGET_S

    assert TTFT_BUDGET_S == pytest.approx(SPEECH_BUDGET_S - sum(SPEECH_STAGES.values()))
    assert SLOS["ttft_p95"].target == TTFT_BUDGET_S, (
        "the SLO must be the derived slice, not a separate hardcoded number")
    assert 0 < TTFT_BUDGET_S < SPEECH_BUDGET_S, (
        "the stages must leave the engine something, and less than the whole")
