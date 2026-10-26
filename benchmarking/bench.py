"""Run one named case and report what every component did, as deltas.

The problem this exists to fix: a notebook that reads cumulative counters is
reporting an average over every request the pod has ever served. 02 once said
"decode recomputes 0.0% of its prompt tokens" on the strength of 29.8M tokens
served out of a prefix cache warmed by hours of earlier load tests — true, and
attributable to nothing we chose.

Every number here is the difference across one case, scraped from the engines
themselves rather than Prometheus, whose 15s interval is too coarse to attribute
a change to a handful of requests.

    from bench import CASES, run_case
    m = run_case(CASES['long_novel'], n=3)
    m.cache_hit_rate      # 0.01 — a prompt it has never seen
    m.from_connector      # 14,970 — the hop carried the whole thing

The cases vary two things only, because those two decide what every component
does: how long the prompt is, and whether its prefix is already resident.
"""
from __future__ import annotations

import json
import urllib.request
import uuid
from dataclasses import dataclass, field

#: Four characters per token for English prose, which is close enough to hit a
#: token budget within a few percent.
CHARS_PER_TOKEN = 4

POOL_PORTS = {"prefill": 30800, "decode": 30801}
GATEWAY = "http://127.0.0.1:8080"

#: The gateway's own guardrail (MAX_PROMPT_CHARS). A case that exceeds it is
#: refused with 400 prompt_too_long before any engine sees it, so the budget has
#: to leave room for the uuid tag and the chat template.
MAX_PROMPT_CHARS = 60_000
TAG_CHARS = 48


def filler(tokens: int) -> str:
    return ("evidence interaction dosage bioavailability clinical review "
            * (tokens * CHARS_PER_TOKEN // 60 + 1))[: tokens * CHARS_PER_TOKEN]


@dataclass(frozen=True)
class Case:
    key: str
    label: str
    prompt_tokens: int
    #: novel  — the engine has never seen this prefix
    #: repeat — sent once to warm it, then measured
    #: partial — shares a prefix with a warmed prompt, then diverges
    prefix: str
    why: str


CASES: dict[str, Case] = {c.key: c for c in (
    Case("short_novel", "short, never seen", 500, "novel",
         "The cheapest thing the cluster can be asked to do, and the only case "
         "where the engine is not reading a long prompt."),
    Case("short_repeat", "short, already cached", 500, "repeat",
         "Isolates the prefix cache from prompt length: same text, second time."),
    Case("long_novel", "long, never seen", 14_000, "novel",
         "The research turn as it actually arrives the first time. Nothing is "
         "cached, so the hop has to carry the whole prompt — this is the case "
         "disaggregation exists for, and the most expensive one."),
    Case("long_repeat", "long, already cached", 14_000, "repeat",
         "The same research turn asked twice. The local cache should serve it "
         "and the connector should fall idle."),
    Case("long_partial", "long, half shared", 13_000, "partial",
         "The realistic middle: a session where the system prompt and sources "
         "repeat but the question is new."),
)}


@dataclass
class Measurement:
    case: Case
    n: int
    ok: int = 0
    shed: int = 0
    errors: int = 0
    #: Why requests failed, by status. A case that silently returns ok=0 is the
    #: failure this utility exists to prevent.
    failures: dict[str, int] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)

    def _d(self, key: str) -> float:
        return self.after.get(key, 0.0) - self.before.get(key, 0.0)

    @property
    def cache_queries(self) -> float:
        return self._d("prefix_queries")

    @property
    def cache_hits(self) -> float:
        return self._d("prefix_hits")

    @property
    def cache_hit_rate(self) -> float:
        q = self.cache_queries
        return self.cache_hits / q if q else 0.0

    @property
    def from_connector(self) -> float:
        return self._d("from_hop")

    @property
    def from_cache(self) -> float:
        return self._d("from_cache")

    @property
    def computed(self) -> float:
        return self._d("computed")

    @property
    def prompt_tokens(self) -> float:
        return self.from_connector + self.from_cache + self.computed

    def _mean(self, stem: str) -> float:
        n = self._d(f"{stem}_count")
        return self._d(f"{stem}_sum") / n if n else 0.0

    @property
    def decode_ttft(self) -> float:
        return self._mean("d_ttft")

    @property
    def prefill_e2e(self) -> float:
        return self._mean("p_e2e")

    @property
    def user_ttft(self) -> float:
        """What a listener waits through: prefill, the hop, then decode's first
        token. Neither pool's own TTFT is this number — they run in series."""
        return self.prefill_e2e + self.decode_ttft

    @property
    def itl(self) -> float:
        return self._mean("itl")

    @property
    def client_mean(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0


def _scrape(pool: str) -> dict[str, float]:
    url = f"http://127.0.0.1:{POOL_PORTS[pool]}/metrics"
    text = urllib.request.urlopen(url, timeout=10).read().decode()
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name] = float(value)
        except ValueError:
            pass
    return out


def counters() -> dict[str, float]:
    """One reading of every number a case can move."""
    d, p = _scrape("decode"), _scrape("prefill")
    sum_of = lambda m, frag: sum(v for k, v in m.items() if frag in k)
    return {
        "prefix_queries": sum_of(d, "vllm:prefix_cache_queries_total"),
        "prefix_hits":    sum_of(d, "vllm:prefix_cache_hits_total"),
        "from_hop":       sum_of(d, 'source="external_kv_transfer"'),
        "from_cache":     sum_of(d, 'source="local_cache_hit"'),
        "computed":       sum_of(d, 'source="local_compute"'),
        "d_ttft_sum":     sum_of(d, "vllm:time_to_first_token_seconds_sum"),
        "d_ttft_count":   sum_of(d, "vllm:time_to_first_token_seconds_count"),
        "p_e2e_sum":      sum_of(p, "vllm:e2e_request_latency_seconds_sum"),
        "p_e2e_count":    sum_of(p, "vllm:e2e_request_latency_seconds_count"),
        "itl_sum":        sum_of(d, "vllm:inter_token_latency_seconds_sum"),
        "itl_count":      sum_of(d, "vllm:inter_token_latency_seconds_count"),
        "preemptions":    sum_of(d, "vllm:num_preemptions_total"),
    }


def prompts_for(case: Case, n: int) -> tuple[list[str], list[str]]:
    """The prompts to measure, and any to send first to set the cache state.

    uuid4, not a seeded RNG: a cache probe needs text the engine has never seen,
    and a seeded prompt is identical between runs — so the second run of a
    notebook measures its own leftovers.
    """
    body = filler(case.prompt_tokens)
    tag = uuid.uuid4()
    if len(body) + TAG_CHARS > MAX_PROMPT_CHARS:
        raise ValueError(
            f"{case.key}: {len(body) + TAG_CHARS:,} chars exceeds the gateway's "
            f"{MAX_PROMPT_CHARS:,}-char guardrail, so every request would be "
            f"refused with 400 prompt_too_long before reaching an engine")

    if case.prefix == "novel":
        # A fresh tag per request, so none of them warms another.
        return [f"[{uuid.uuid4()}] {body}" for _ in range(n)], []
    if case.prefix == "repeat":
        text = f"[{tag}] {body}"
        return [text] * n, [text]          # warm once, then measure
    if case.prefix == "partial":
        # Genuinely half. Appending a 200-token suffix to a 13,000-token prefix
        # is 98% shared, which measures the same thing as "repeat" while being
        # labelled differently — the realistic middle has to actually sit in the
        # middle.
        half = case.prompt_tokens // 2
        shared = f"[{tag}] {filler(half)}"
        return ([f"{shared}\n\n[{uuid.uuid4()}] {filler(half)}" for _ in range(n)],
                [shared])
    raise ValueError(f"unknown prefix mode: {case.prefix}")


def _post(prompt: str, *, max_tokens: int,
          timeout: float) -> tuple[int, float, str]:
    import time
    body = json.dumps({
        # The alias, not a model name: the gateway resolves it to whatever it
        # serves. Pinning "...7B..." here sent every benchmark request to a 32B
        # fleet as model_not_allowed, and the run came back as all-errors.
        "model": "text",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(f"{GATEWAY}/v1/chat/completions", body,
                                 {"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status, time.time() - t0, ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.loads(e.read()).get("error", {}).get("type")
                      or e.reason or "")
        except Exception:
            detail = e.reason or ""
        return e.code, time.time() - t0, f"{e.code} {detail}"
    except Exception as e:
        return 0, time.time() - t0, f"{type(e).__name__}: {e}"


#: The router refuses with 503 above this much KV on either pool
#: (KV_SATURATION in gateway/types.py). A case started while the previous one is
#: still draining is measured against a cluster that is already refusing work.
SHED_ABOVE = 0.80


def settle(*, timeout_s: float = 120.0, poll_s: float = 2.0) -> dict[str, float]:
    """Wait until nothing is queued and KV has fallen clear of the shed line.

    Without this, running the cases in sequence leaves prefill above 80% and the
    next case's warm-up is refused — which made a whole case report zeros with
    no indication that the cluster, not the code, had declined.
    """
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        state = {}
        for pool in POOL_PORTS:
            m = _scrape(pool)
            pick = lambda frag: max((v for k, v in m.items() if frag in k), default=0.0)
            state[f"{pool}_kv"] = pick("vllm:kv_cache_usage_perc")
            state[f"{pool}_waiting"] = pick("vllm:num_requests_waiting")
            state[f"{pool}_running"] = pick("vllm:num_requests_running")
        busy = any(v > 0 for k, v in state.items() if k.endswith(("waiting", "running")))
        hot = any(v >= SHED_ABOVE for k, v in state.items() if k.endswith("_kv"))
        if (not busy and not hot) or time.monotonic() >= deadline:
            return state
        time.sleep(poll_s)


def run_case(case: Case, *, n: int = 3, max_tokens: int = 32,
             timeout: float = 300.0, wait: bool = True) -> Measurement:
    """Send one case and return what each component did across it."""
    if wait:
        settle()
    measured, warmup = prompts_for(case, n)
    for prompt in warmup:
        status, _, why = _post(prompt, max_tokens=max_tokens, timeout=timeout)
        if status != 200:
            raise RuntimeError(
                f"{case.key}: the warm-up request failed ({why}), so the cache "
                f"state this case depends on was never established")

    m = Measurement(case=case, n=n, before=counters())
    for prompt in measured:
        status, seconds, why = _post(prompt, max_tokens=max_tokens, timeout=timeout)
        if status == 200:
            m.ok += 1
            m.latencies.append(seconds)
            continue
        if status in (429, 503):
            m.shed += 1
        else:
            m.errors += 1
        m.failures[why] = m.failures.get(why, 0) + 1
    m.after = counters()
    return m
