"""An agent crew hammering the gateway, and what happens to it when refused.

This is the experiment. An agent makes many small LLM calls in bursts; under load every
call slows down, including the ones already running. Admission control refuses some so
the rest stay fast. The question is whether the agent ends up better off.

    python3 -m benchmarking.agent_flood --url http://127.0.0.1:8080 --rps 20 --for 2m

Replaces app/crew_flood.py, which cannot answer that question for two reasons:

  ThreadPoolExecutor(max_workers=8) is a CLOSED LOOP. It holds eight calls in flight and
  starts a new one as each finishes, so it slows down exactly as much as the server does.
  The queue never grows and there is nothing for a policy to shed. Overload means
  arrivals outpacing service, which requires arrivals that do not wait for completions.

  A 503 ended the run. `fut.result()` re-raises, so the first refusal killed the flood —
  the agent could not survive the policy working, which is the only case worth measuring.

An agent facing a refusal has choices, and which one it takes changes the answer:

  give-up   the call is lost. Honest, and what a naive client does
  retry     with jittered backoff. The work still happens, later

--on-refusal picks one, and the report counts them separately, because "refused 26%"
means something very different if most of those succeeded on a second attempt.
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: The crew. Four roles with distinct prompts, so no two calls share a prefix and the
#: engine's prefix cache cannot make the second one nearly free — a shared prefix is
#: almost free to serve and leaves no KV footprint, which would flatten the very signal
#: a policy reads.
ROLES = (
    ("researcher", "List two facts about GPU KV cache in one sentence."),
    ("writer", "Rewrite this for a slide: prefix cache should stick to a replica."),
    ("reviewer", "Approve or reject: shedding a 429 must stay local, never overflow."),
    ("planner", "One sentence: when should a decode replica be added?"),
)

#: In-flight backstop, NOT a concurrency target. Hitting it means the arrival rate outran
#: the engine by more than this, which is reported rather than silently throttled — a
#: capped generator is a closed loop wearing a disguise.
IN_FLIGHT_CAP = 2048

#: Consecutive failures with nothing served before giving up. Catches a wrong URL or model
#: in seconds instead of spending the window recording the same error at the right rate.
ABORT_AFTER = 25


class Crew:
    """Offers agent calls at a fixed rate and records what each one got."""

    def __init__(
        self,
        url: str,
        out: str | Path | None,
        *,
        model: str = "text",
        rps: float = 20.0,
        max_tokens: int = 32,
        on_refusal: str = "retry",
        retries: int = 2,
        backoff_ms: float = 250.0,
        timeout: float = 120.0,
        seed: int = 0,
    ) -> None:
        if on_refusal not in ("retry", "give-up"):
            raise ValueError(f"on_refusal must be retry or give-up, not {on_refusal!r}")
        self.url = url.rstrip("/")
        self.out = Path(out) if out else None
        self.model = model
        self.rps = rps
        self.max_tokens = max_tokens
        self.on_refusal = on_refusal
        self.retries = retries if on_refusal == "retry" else 0
        self.backoff_ms = backoff_ms
        self.timeout = timeout
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._in_flight = 0
        self.records: list[dict[str, object]] = []

        self.offered = 0
        self.served = 0           #: 200 on the first attempt
        self.served_on_retry = 0  #: refused, then succeeded. Work the agent still got
        self.refused = 0          #: refused and never served
        self.failed = 0           #: not a refusal — a timeout, a 500, a wrong model
        self.at_capacity = 0
        self.aborted: str | None = None

    # -- one agent call -----------------------------------------------------

    def call(self, request_id: str, role: str, prompt: str) -> dict[str, object]:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": f"[{role}] {prompt}"}],
                "max_tokens": self.max_tokens,
                "tenant": "crew",
            }
        ).encode()

        rec: dict[str, object] = {
            "request_id": request_id,
            "at": time.time(),
            "role": role,
            "attempts": 0,
        }
        started = time.monotonic()

        for attempt in range(self.retries + 1):
            rec["attempts"] = attempt + 1
            try:
                req = urllib.request.Request(
                    f"{self.url}/v1/chat/completions",
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp.read()
                    rec |= {
                        "status": resp.status,
                        # Timed from the FIRST attempt, because that is when the agent
                        # wanted an answer. Timing only the winning attempt would hide
                        # the cost of having been refused.
                        "latency_ms": (time.monotonic() - started) * 1000.0,
                        "refused": False,
                        "served": True,
                    }
                    return rec
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 503):
                    rec |= {
                        "status": exc.code,
                        "latency_ms": (time.monotonic() - started) * 1000.0,
                        "served": False,
                        "error": "http",
                    }
                    return rec
                rec["status"] = exc.code
                rec["refused"] = True
                if attempt < self.retries:
                    # Exponential and jittered. A crew retrying in lockstep re-creates
                    # the burst the policy just shed.
                    wait = self.backoff_ms * (2**attempt) * (0.5 + self._rng.random())
                    time.sleep(wait / 1000.0)
                    continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                rec |= {
                    "latency_ms": (time.monotonic() - started) * 1000.0,
                    "served": False,
                    "error": str(exc),
                }
                return rec

        rec |= {"latency_ms": (time.monotonic() - started) * 1000.0, "served": False}
        return rec

    # -- the run ------------------------------------------------------------

    def run(self, seconds: float) -> Crew:
        """Offer calls for `seconds`, then wait for whatever is still in flight.

        Arrival gaps are exponential, so calls bunch the way a crew's really do — a
        planner fanning out to four workers at once, not a metronome.
        """
        threads: list[threading.Thread] = []
        deadline = time.monotonic() + seconds

        while time.monotonic() < deadline:
            time.sleep(self._rng.expovariate(self.rps))
            if self.aborted:
                break
            if self.failed >= ABORT_AFTER and self.served == 0 and self.refused == 0:
                self.aborted = (
                    f"{self.failed} calls failed and none were served — wrong --url, "
                    "wrong --model, or the gateway is not up"
                )
                break
            with self._lock:
                if self._in_flight >= IN_FLIGHT_CAP:
                    self.at_capacity += 1
                    continue
                self._in_flight += 1
            self.offered += 1
            role, prompt = ROLES[self.offered % len(ROLES)]
            t = threading.Thread(
                target=self._work,
                args=(f"a{self.offered:06d}", role, prompt),
                daemon=True,
            )
            t.start()
            threads.append(t)

        # Calls outlive the arrival window. Counting only what finished inside it would
        # drop the slowest, which are the ones the experiment is about.
        for t in threads:
            t.join(timeout=self.timeout + 60)

        if self.out:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            with self.out.open("w") as fh:
                for rec in self.records:
                    fh.write(json.dumps(rec) + "\n")
        return self

    def _work(self, request_id: str, role: str, prompt: str) -> None:
        try:
            rec = self.call(request_id, role, prompt)
        finally:
            with self._lock:
                self._in_flight -= 1
        with self._lock:
            self.records.append(rec)
            if rec.get("served"):
                if int(rec.get("attempts", 1)) > 1:
                    self.served_on_retry += 1
                else:
                    self.served += 1
            elif rec.get("refused"):
                self.refused += 1
            else:
                self.failed += 1


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="An agent crew against the gateway.")
    p.add_argument("--url", default="http://127.0.0.1:8080", help="the GATEWAY")
    p.add_argument("--model", default="text")
    p.add_argument("--rps", type=float, default=20.0, help="calls OFFERED per second")
    p.add_argument("--for", dest="seconds", default="2m")
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument(
        "--on-refusal",
        choices=["retry", "give-up"],
        default="retry",
        help="what the agent does with a 503. retry is what a real one does",
    )
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("-o", "--out", default="traces/agent.jsonl")
    a = p.parse_args(argv)

    seconds = float(a.seconds[:-1]) * 60 if a.seconds.endswith("m") else float(a.seconds)
    print(f"crew: {a.rps:g} calls/s for {seconds:g}s -> {a.url}  (on refusal: {a.on_refusal})")

    crew = Crew(
        a.url,
        a.out,
        model=a.model,
        rps=a.rps,
        max_tokens=a.max_tokens,
        on_refusal=a.on_refusal,
        retries=a.retries,
        seed=a.seed,
    ).run(seconds)

    got = crew.served + crew.served_on_retry
    print(f"\n  offered           {crew.offered}")
    print(f"  served            {crew.served}")
    print(f"  served on retry   {crew.served_on_retry}   (refused, then succeeded)")
    print(f"  refused for good  {crew.refused}")
    print(f"  failed            {crew.failed}")
    if crew.offered:
        # Of OFFERED, not of admitted. Dividing by admitted rewards refusing more rather
        # than serving better.
        print(f"\n  completion rate   {got / crew.offered:.1%} of offered")

    lat = [float(r["latency_ms"]) for r in crew.records if r.get("served")]
    if lat:
        print(f"  latency p50       {percentile(lat, 0.50):.0f} ms")
        print(f"  latency p95       {percentile(lat, 0.95):.0f} ms")
        print(f"  latency p99       {percentile(lat, 0.99):.0f} ms")
    if crew.at_capacity:
        print(f"\n  {crew.at_capacity} arrival(s) dropped at the {IN_FLIGHT_CAP}-in-flight cap")
    if crew.aborted:
        print(f"\ngave up: {crew.aborted}")
        return 1
    if got == 0 and crew.refused == 0:
        print(f"\nnothing was served. Is {a.url} the gateway, and --model right?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
