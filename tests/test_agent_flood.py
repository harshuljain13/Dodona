"""The agent crew, against a gateway that refuses.

Every assertion here is about a bug in app/crew_flood.py, the version this replaces: a
closed loop that cannot overload anything, and a 503 that ended the run.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from edge.agent_flood import Crew

REFUSE_EVERY = 2  #: refuse every Nth call, so both paths run in one pass


class FakeGateway(BaseHTTPRequestHandler):
    seen = 0
    mode = "mixed"  # mixed | always | never
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's spelling
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with FakeGateway.lock:
            FakeGateway.seen += 1
            n = FakeGateway.seen

        refuse = FakeGateway.mode == "always" or (
            FakeGateway.mode == "mixed" and n % REFUSE_EVERY == 0
        )
        if refuse:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        """Silence. The handler's default writes to stderr on every request."""


@pytest.fixture
def gateway():
    def start(mode: str) -> str:
        FakeGateway.seen = 0
        FakeGateway.mode = mode
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_port}"

    started: list[ThreadingHTTPServer] = []
    yield start
    for srv in started:
        srv.shutdown()


# --------------------------------------------------------------------------
# A refusal is an outcome, not the end of the run
# --------------------------------------------------------------------------


def test_a_refusal_does_not_end_the_run(gateway) -> None:
    """crew_flood called `fut.result()`, which re-raises, so the first 503 killed the
    flood. The agent could not survive the policy working — the only case worth
    measuring."""
    crew = Crew(gateway("always"), None, rps=60, on_refusal="give-up").run(0.4)

    assert crew.offered > 5
    assert crew.refused == crew.offered
    # A refusal is not a failure. Counting it as one would make a working policy
    # indistinguishable from a broken load generator.
    assert crew.failed == 0


def test_retry_turns_refusals_into_served_calls(gateway) -> None:
    """What a real agent does, and it changes what "refused 50%" means: if most of those
    land on a second attempt, the work still happened."""
    crew = Crew(gateway("mixed"), None, rps=60, on_refusal="retry", retries=3).run(0.4)

    assert crew.served_on_retry > 0
    # No double counting — every offered call lands in exactly one bucket, or the
    # completion rate is a number nobody can reconstruct.
    assert crew.served + crew.served_on_retry + crew.refused + crew.failed == crew.offered


def test_give_up_does_not_retry(gateway) -> None:
    crew = Crew(gateway("mixed"), None, rps=60, on_refusal="give-up").run(0.4)
    assert crew.served_on_retry == 0


# --------------------------------------------------------------------------
# Open loop, or there is no overload to admit against
# --------------------------------------------------------------------------


def test_arrivals_do_not_wait_for_completions(gateway) -> None:
    """The property crew_flood lacked. A pool of eight workers is bounded by completions,
    so it slows down exactly as much as the server does, the queue never grows, and
    admission control has nothing to shed."""
    crew = Crew(gateway("never"), None, rps=80).run(0.5)
    assert crew.offered >= 20


def test_an_unreachable_gateway_gives_up_early() -> None:
    """A wrong URL used to burn the whole window recording the same error at exactly the
    requested arrival rate, which reads as a run that happened."""
    from edge.agent_flood import ABORT_AFTER

    crew = Crew("http://127.0.0.1:1", None, rps=200).run(10)

    assert crew.aborted is not None
    assert "none were served" in crew.aborted
    assert crew.offered < ABORT_AFTER * 20


# --------------------------------------------------------------------------
# What gets written down
# --------------------------------------------------------------------------


def test_latency_is_timed_from_the_first_attempt(gateway, tmp_path: Path) -> None:
    """That is when the agent wanted an answer. Timing only the winning attempt would
    hide the cost of having been refused — which is exactly the cost the experiment is
    trying to weigh against faster tails."""
    out = tmp_path / "agent.jsonl"
    Crew(gateway("mixed"), out, rps=40, on_refusal="retry", retries=3).run(0.4)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    retried = [r for r in rows if r.get("served") and int(r["attempts"]) > 1]
    first_try = [r for r in rows if r.get("served") and int(r["attempts"]) == 1]
    assert retried and first_try
    assert min(float(r["latency_ms"]) for r in retried) > min(
        float(r["latency_ms"]) for r in first_try
    )


def test_every_call_is_recorded(gateway, tmp_path: Path) -> None:
    out = tmp_path / "agent.jsonl"
    crew = Crew(gateway("mixed"), out, rps=50, on_refusal="give-up").run(0.3)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == crew.offered
    assert all("request_id" in r and "role" in r for r in rows)


def test_prompts_differ_by_role(gateway) -> None:
    """A shared prefix is nearly free to serve and leaves no KV footprint, so a crew
    repeating one prompt reports a cache that never fills at any rate."""
    from edge.agent_flood import ROLES

    prompts = {p for _, p in ROLES}
    assert len(prompts) == len(ROLES)


def test_an_unknown_refusal_strategy_is_refused() -> None:
    with pytest.raises(ValueError, match="on_refusal"):
        Crew("http://127.0.0.1:1", None, on_refusal="panic")
