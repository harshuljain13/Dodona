"""What Dodona promises, in one place.

Every notebook checks against these, and 99-report aggregates them. In prose an
SLO drifts between documents; here a change breaks a check.

    from slos import SLOS, target_sessions, set_target_sessions

The point of an SLO rather than a measurement: "p95 TTFT was 0.9s" is a number,
"0.9s against an 800ms budget" is a result. A config that is faster but misses
the budget under load is still a failure.

THE SLO IS A CONSTANT. Capacity is what you derive from it, and admission
control is what keeps offered load under that capacity so the constant holds. A
measured 2.4s TTFT is not a reason to move the target to 2.4s — it is a reason
to serve fewer sessions, which is what shedding is for. Relaxing a promise
because the system missed it turns every later comparison into a tautology.

Note which number the engine is held to. A listener tolerates about 1.5s of
silence, but that covers ASR, the engine and TTS in series — the engine gets a
slice, not the whole thing. See SPEECH_STAGES.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Written by 02-capacity, read by everything after it. Derived, not guessed —
#: a concurrency target picked before measuring the saturation curve is a wish.
DERIVED = ROOT / "benchmarking" / "slos-derived.json"


@dataclass(frozen=True)
class SLO:
    key: str
    label: str
    target: float
    unit: str
    #: "max" — measured must not exceed target. "min" — must not fall below.
    direction: str
    why: str

    def met(self, measured: float) -> bool:
        return measured <= self.target if self.direction == "max" else measured >= self.target

    def describe(self, measured: float) -> str:
        op = "<=" if self.direction == "max" else ">="
        return f"{measured:g}{self.unit} {op} {self.target:g}{self.unit}"


#: One chain, one budget. The only promise a listener experiences is WHEN THEY
#: HEAR AUDIO, and these stages are serial on the way there:
#:
#:     ASR finalize -> LLM TTFT -> TTS first chunk -> playback
#:
#: Stated as a decomposition rather than one number, because a figure whose
#: derivation lives in prose drifts from the figure the code checks.
SPEECH_BUDGET_S = 1.5

SPEECH_STAGES: dict[str, float] = {
    "ASR endpoint and finalize": 0.300,
    "TTS first chunk": 0.200,
    "network and orchestration": 0.200,
}

#: What is left for the engine once the rest of the path is paid for. Derived,
#: so changing a stage moves the SLO instead of silently contradicting it.
TTFT_BUDGET_S = round(SPEECH_BUDGET_S - sum(SPEECH_STAGES.values()), 3)

#: Speech is consumed at about 3.3 tokens/s (150 wpm at ~0.75 words per token).
#: Slower than ~300ms per token and TTS runs dry mid-sentence — the audio
#: audibly stutters. That is the hard floor; the target keeps roughly 4x buffer
#: so a hiccup never reaches the listener.
SPEECH_TOKENS_PER_S = 3.3
ITL_STUTTER_S = round(1.0 / SPEECH_TOKENS_PER_S, 3)
ITL_BUDGET_S = 0.080


SLOS: dict[str, SLO] = {
    "ttft_p95": SLO(
        "ttft_p95", "TTFT p95", TTFT_BUDGET_S, "s", "max",
        "A voice agent's latency is dead air, and the listener's 1.5s covers the "
        f"whole path: ASR, the engine, then TTS. {TTFT_BUDGET_S:g}s is what remains "
        f"for first token once {sum(SPEECH_STAGES.values()):g}s of the rest is paid "
        "for.",
    ),
    "itl_p95": SLO(
        "itl_p95", "inter-token latency p95", ITL_BUDGET_S, "s", "max",
        f"Speech is spoken at {SPEECH_TOKENS_PER_S:g} tokens/s, so past "
        f"{ITL_STUTTER_S:g}s per token TTS runs dry and the audio stutters "
        "mid-sentence. The target holds about 4x that buffer.",
    ),
    "answer_p95": SLO(
        "answer_p95", "full answer p95", 30.0, "s", "max",
        "A research turn runs tool calls and reads sources. 30s is the budget for "
        "the whole turn. Generation outpaces speech roughly 40x here, so once "
        "audio starts the listener is listening rather than waiting — this is a "
        "regression guard, not the binding constraint.",
    ),
    "preemptions": SLO(
        "preemptions", "preemptions", 0, "", "max",
        "vLLM preempting a sequence is a stall mid-sentence in a spoken answer. "
        "This is the failure admission control exists to prevent.",
    ),
    "shed_rate": SLO(
        "shed_rate", "shed rate at target load", 0.05, "", "max",
        "503s are allowed but rare. Shedding early beats preempting; a system "
        "shedding 30% is not serving. Note this applies AT the derived capacity — "
        "above it, shedding is the system working as intended.",
    ),
}


def target_sessions() -> int | None:
    """Concurrent sessions that must hold the latency SLOs.

    None until 02-capacity measures the saturation curve and writes it. A
    notebook that needs it and finds None should say so rather than assume.
    """
    if not DERIVED.exists():
        return None
    try:
        return int(json.loads(DERIVED.read_text())["target_sessions"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def set_target_sessions(n: int, *, evidence: str) -> Path:
    """Record the derived target. `evidence` is how it was arrived at."""
    DERIVED.parent.mkdir(parents=True, exist_ok=True)
    DERIVED.write_text(json.dumps(
        {"target_sessions": int(n), "evidence": evidence}, indent=2) + "\n")
    return DERIVED


def budget_rows() -> list[list[object]]:
    """The speech budget, stage by stage, with the engine's share derived."""
    rows = [[name, f"{s * 1000:.0f} ms", ""] for name, s in SPEECH_STAGES.items()]
    rows.insert(1, ["LLM TTFT", f"{TTFT_BUDGET_S * 1000:.0f} ms",
                    "what the engine is held to"])
    rows.append(["first audio", f"{SPEECH_BUDGET_S * 1000:.0f} ms",
                 "what a listener tolerates"])
    return rows


def table_rows() -> list[list[object]]:
    """For Report.table — the promises, as declared."""
    rows = [[s.label, f"{s.target:g}{s.unit}",
             "max" if s.direction == "max" else "min"] for s in SLOS.values()]
    n = target_sessions()
    rows.append(["concurrent sessions", n if n is not None else "derived in 02", "min"])
    return rows
