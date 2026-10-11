#!/usr/bin/env python3
"""Agent-shaped load, and the signal reading that goes with it.

The brief is explicit that the traffic must look like the app: "requests should
look like RAG retrieves or agent steps, not anonymous gibberish". So this does
not send uniform prompts. It replays **sessions**, and a session grows:

    turn 1   ~1,040 tok   1k shared prefix + the spoken question
    turn 2   ~1,900 tok   + search results
    turn 3  ~14,000 tok   + one fetched source, the expensive turn
    turn 4  ~15,400 tok   + synthesis, the worst case

The ~1k prefix is byte-identical across every turn and every session, which is
what gives prefix caching something to do — and what makes placement that
ignores cache locality visibly worse.

**It samples `vllm:kv_cache_usage_perc` throughout.** A latency number without
the signal range beside it cannot answer the only question that matters: was
anything actually scarce while this ran? A policy whose signal never moved is
indistinguishable from one that worked.

    python3 benchmarking/load.py --url http://127.0.0.1:30801 --sessions 8
    python3 benchmarking/load.py --url http://127.0.0.1:8080 --sessions 14 --run high-a

Stdlib only, so it runs on the box without installing anything.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

#: benchmarking/load.py -> repo root
ROOT = Path(__file__).resolve().parents[1]

# ~1k tokens of system prompt and tool schemas. Identical every turn, every
# session — this is the shared prefix the whole prefix-cache story rests on.
PREFIX = (
    "You are a research assistant for herbs and supplements. Plan, then use the "
    "tools to find and read sources. Cite every claim with the URL you read it "
    "from. Never state a dosage as advice; report what a source says and name "
    "the source. Tools: web_search(query) -> results; scrape_url(url) -> text; "
    "http_request(url) -> body; calculator(expression) -> number. "
) * 24

QUESTIONS = [
    "which herbs interact with warfarin?",
    "does st john's wort affect the pill?",
    "is ashwagandha safe with thyroid medication?",
    "what does the evidence say about turmeric and absorption?",
    "can magnesium be taken with blood pressure medication?",
    "does milk thistle interact with statins?",
]

#: Roughly four characters per token for English prose. Good enough to hit a
#: token budget within a few percent, which is all the shape needs.
CHARS_PER_TOKEN = 4


def filler(tokens: int) -> str:
    return ("evidence interaction dosage bioavailability clinical review " *
            (tokens * CHARS_PER_TOKEN // 60 + 1))[: tokens * CHARS_PER_TOKEN]


def turns(question: str) -> list[str]:
    """One session: four prompts, each containing the last."""
    t1 = f"{PREFIX}\n\nUser: {question}"
    t2 = f"{t1}\n\nweb_search results:\n{filler(800)}"
    t3 = f"{t2}\n\nscrape_url returned:\n{filler(12000)}"
    t4 = f"{t3}\n\nNow answer, with citations."
    return [t1, t2, t3, t4]


@dataclass
class Result:
    session: int
    turn: int
    prompt_chars: int
    status: int
    latency_s: float
    completion_tokens: int = 0
    prompt_tokens: int = 0
    error: str = ""


@dataclass
class Run:
    results: list[Result] = field(default_factory=list)
    signal: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def post(url: str, model: str, prompt: str, max_tokens: int, timeout: float):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "top_p": 0.8,
    }).encode()
    req = urllib.request.Request(f"{url}/v1/chat/completions", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def session(idx: int, args, run: Run) -> None:
    """One agent's conversation, repeated `--repeat` times.

    A single pass is four turns and finishes in about twelve seconds. Prometheus
    scrapes every 15s, so a one-pass run can land entirely between two scrapes
    and leave no trace in the metrics at all — which is how a capacity sweep
    produced decode-KV readings of 0%, 51%, 7.5%, 52% across monotonically
    rising load. Repeating holds the load up long enough to be observed.
    """
    q = QUESTIONS[idx % len(QUESTIONS)]
    prompts = [p for _ in range(max(1, args.repeat)) for p in turns(q)]
    for n, prompt in enumerate(prompts, start=1):
        t0 = time.time()
        try:
            status, data = post(args.url, args.model, prompt, args.max_tokens, args.timeout)
            usage = data.get("usage") or {}
            res = Result(idx, n, len(prompt), status, time.time() - t0,
                         usage.get("completion_tokens", 0), usage.get("prompt_tokens", 0))
        except urllib.error.HTTPError as e:
            # 429 and 503 are the interesting outcomes once admission exists —
            # they are results, not failures, and must be counted separately.
            res = Result(idx, n, len(prompt), e.code, time.time() - t0,
                         error=e.reason or "")
        except Exception as e:
            res = Result(idx, n, len(prompt), 0, time.time() - t0,
                         error=f"{type(e).__name__}: {e}")
        with run.lock:
            run.results.append(res)
        # The agent goes quiet here while a tool call runs. Load is bursty
        # because of how the app works, not because we configured a think time.
        time.sleep(args.tool_gap)


#: The signal an admission policy reads, asked of Prometheus rather than of one
#: pod. KV usage is per-engine, so the figure that matters across a fleet is the
#: max — a policy fires on the worst slice, not on the average of all of them.
SIGNAL_QUERY = "max(vllm:kv_cache_usage_perc)"


def sample_signal(prom_url: str, run: Run, stop: threading.Event) -> None:
    """Poll the KV-usage gauge. Without this a run says nothing about scarcity.

    This used to scrape `<gateway>/metrics`, which does not export
    vllm:kv_cache_usage_perc at all — that gauge lives on the engine pods. So it
    silently collected nothing and every run reported "no signal samples",
    meaning no load run ever recorded whether the GPU was under pressure. A
    latency number without the scarcity beside it cannot tell a cluster that
    coped from one that was never asked for anything.
    """
    url = f"{prom_url.rstrip('/')}/api/v1/query?query={urllib.parse.quote(SIGNAL_QUERY)}"
    while not stop.is_set():
        try:
            body = json.loads(urllib.request.urlopen(url, timeout=5).read())
            result = body.get("data", {}).get("result") or []
            if result:
                with run.lock:
                    run.signal.append(float(result[0]["value"][1]))
        except Exception:
            pass
        stop.wait(1.0)


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8080",
                    help="serve path. The gateway once it exists; a pool NodePort before that")
    # Prometheus, not the gateway: the KV gauge is on the engines, and Prometheus
    # is the only place that already aggregates across them.
    ap.add_argument("--prom-url", default="http://127.0.0.1:31490",
                    help="where to read the scarcity signal from")
    ap.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct-AWQ")
    ap.add_argument("--sessions", type=int, default=8)
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--tool-gap", type=float, default=1.0, help="quiet time between turns")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--repeat", type=int, default=1,
                    help="passes through the conversation; raise it so the run "
                         "spans several 15s Prometheus scrapes")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--run", default="", help="write the bundle to metrics/<run>/")
    args = ap.parse_args()
    random.seed(args.seed)


    run, stop = Run(), threading.Event()
    sampler = threading.Thread(target=sample_signal, args=(args.prom_url, run, stop), daemon=True)
    sampler.start()

    print(f"  {args.sessions} sessions x {4 * max(1, args.repeat)} turns "
          f"-> {args.url}")
    t0 = time.time()
    threads = [threading.Thread(target=session, args=(i, args, run))
               for i in range(args.sessions)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    stop.set()
    sampler.join(timeout=3)

    ok = [r for r in run.results if r.status == 200]
    shed = [r for r in run.results if r.status in (429, 503, 529)]
    err = [r for r in run.results if r.status not in (200, 429, 503, 529)]
    lat = [r.latency_s for r in ok]

    print(f"\n  wall {wall:.1f}s   {len(ok)} ok   {len(shed)} shed   {len(err)} error")
    if lat:
        print(f"  latency  p50 {pct(lat,.5):.2f}s   p95 {pct(lat,.95):.2f}s   max {max(lat):.2f}s")
    if ok:
        pt = sum(r.prompt_tokens for r in ok)
        ct = sum(r.completion_tokens for r in ok)
        print(f"  tokens   {pt:,} prompt / {ct:,} completion   ratio {pt/max(ct,1):.1f}:1")
    for code in sorted({r.status for r in shed}):
        print(f"  shed     {code} x {sum(1 for r in shed if r.status == code)}")
    if err:
        print(f"  first error: {err[0].error[:120]}")

    # The headline. Latency without this cannot say whether anything was scarce.
    if run.signal:
        s = run.signal
        print(f"\n  kv_cache_usage_perc over {len(s)} samples")
        print(f"    min {min(s):.4f}  p50 {pct(s,.5):.4f}  p95 {pct(s,.95):.4f}  max {max(s):.4f}")
        verdict = "REACHED 0.90 — the policy could fire" if max(s) >= 0.90 else \
                  "never reached 0.90 — a kv_threshold policy would have been INERT"
        print(f"    {verdict}")
    else:
        print(f"\n  no signal samples — is Prometheus reachable at {args.prom_url}?")

    if args.run:
        # Repo-root relative, not cwd relative: this is run from the box's
        # home dir as often as from the repo root, and a bundle written to the
        # wrong place is a run you cannot compare against.
        d = str(ROOT / "infra" / "metrics" / args.run)
        os.makedirs(d, exist_ok=True)
        json.dump({"args": vars(args), "wall_s": wall,
                   "signal": run.signal,
                   "results": [asdict(r) for r in run.results]},
                  open(os.path.join(d, "load.json"), "w"), indent=2)
        print(f"\n  bundle -> {d}/load.json")
    return 0 if not err else 1


if __name__ == "__main__":
    raise SystemExit(main())
