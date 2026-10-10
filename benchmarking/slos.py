"""What Dodona promises, in one place.

Every notebook checks against these, and 99-report aggregates them. In prose an
SLO drifts between documents; here a change breaks a check.

    from slos import SLOS, target_sessions, set_target_sessions

The point of an SLO rather than a measurement: "p95 TTFT was 1.2s" is a number,
"1.2s against a 1.5s promise" is a result. A config that is faster but misses
the promise under load is still a failure.
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


SLOS: dict[str, SLO] = {
    "ttft_p95": SLO(
        "ttft_p95", "TTFT p95", 1.5, "s", "max",
        "A voice agent's latency is dead air. Past about 1.5s the listener "
        "starts talking over it.",
    ),
    "turn_p95": SLO(
        "turn_p95", "turn p95", 30.0, "s", "max",
        "A research turn runs tool calls and reads sources. 30s is the budget "
        "for the whole turn, not one forward pass.",
    ),
    "preemptions": SLO(
        "preemptions", "preemptions", 0, "", "max",
        "vLLM preempting a sequence is a stall mid-sentence in a spoken "
        "answer. This is the failure admission control exists to prevent.",
    ),
    "shed_rate": SLO(
        "shed_rate", "shed rate at target load", 0.05, "", "max",
        "503s are allowed but rare. Shedding early beats preempting; a system "
        "shedding 30% is not serving.",
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


def table_rows() -> list[list[object]]:
    """For Report.table — the promises, as declared."""
    rows = [[s.label, f"{s.target:g}{s.unit}",
             "max" if s.direction == "max" else "min"] for s in SLOS.values()]
    n = target_sessions()
    rows.append(["concurrent sessions", n if n is not None else "derived in 02", "min"])
    return rows
