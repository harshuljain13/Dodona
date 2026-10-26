"""Locust load for the demo: make the gateway's decisions visible in a UI.

    make fleet-runpod-ssh                 # the tunnel, in its own terminal
    locust -f benchmarking/locustfile.py  # then open :8089

For *watching*, not measuring. It runs on your Mac through the tunnel, so your
WAN round-trip is inside every latency here — `make load` runs on the box when
you want numbers.

## The number that decides whether this shows anything

A request occupies KV for as long as it is resident: prompt tokens plus what it
has generated. So the pool fills when

    concurrent requests x tokens each  >=  0.90 x pool_tokens

and the engine will only run `MAX_NUM_SEQS` (64) at once. On 2 x A100-80 with
the 32B that pool is ~217,000 tokens per engine, so:

    64 concurrent x  3,250 tokens  =  208,000   fills it
    64 concurrent x     42 tokens  =    2,700   0.012 of the pool

The prompts here used to be "Write one sentence about a GPU." — about seven
tokens, with `max_tokens` 32. Reaching the threshold with those would have
needed **4,650 concurrent requests** against a cap of 64: this file could not
demonstrate admission control, by a factor of 36, and nothing said so. A run
would simply show every request succeeding and the policy never firing, which
reads as "admission control does nothing".

`LOAD_PROMPT_TOKENS` is therefore the most important knob in this file. The
other way to get there is `SLICE_MIB`, which shrinks the pool instead of
growing the requests — see `.env.example`.

## No vision task

There used to be one, at 1/9 of the traffic, posting a 1x1 PNG to `model:
vision`. This fleet loads ONE model per card and it is the text model, so the
gateway advertises `vision` from its allowlist while no engine can serve it —
every one of those requests failed, and a permanent 11% error rate in the UI
teaches you to ignore the error column.
"""
from __future__ import annotations

import os
import random

from locust import HttpUser, between, events, task

#: Roughly four characters per token for this tokenizer. Long enough by default
#: that ~60 concurrent users reach the threshold on a whole-card 32B.
PROMPT_TOKENS = int(os.environ.get("LOAD_PROMPT_TOKENS", "3000"))
MAX_TOKENS = int(os.environ.get("LOAD_MAX_TOKENS", "256"))

#: Distinct per request, so prefix caching does not quietly serve everything
#: from one engine's cache and flatten the very pressure we are trying to make.
#: `LOAD_SHARED_PREFIX=1` inverts that, to show prefix reuse instead.
SHARED_PREFIX = os.environ.get("LOAD_SHARED_PREFIX") == "1"

QUESTIONS = (
    "Summarise the passage above in one sentence.",
    "What is the single most important claim above?",
    "Name one thing the passage above does not explain.",
    "Rewrite the first idea above as a question.",
)

#: Filler that looks like a scraped page, which is what the agent actually
#: sends. Built once: regenerating this per request makes the load generator the
#: bottleneck instead of the fleet.
#:
#: Sized in CHARACTERS, at ~4 per token. The first version joined
#: PROMPT_TOKENS *words*, and these words average six characters, so it
#: overshot by 1.9x — a file whose entire purpose is this arithmetic got its own
#: arithmetic wrong, and nothing would have shown it: the run would simply have
#: saturated sooner than the printout claimed.
_WORDS = ("cache", "token", "prefill", "decode", "latency", "throughput",
          "replica", "gateway", "admission", "tensor", "kernel", "batch")


def _filler(tokens: int) -> str:
    target = tokens * 4
    out: list[str] = []
    size = 0
    while size < target:
        word = random.choice(_WORDS)
        out.append(word)
        size += len(word) + 1
    return " ".join(out)


_FILLER = _filler(PROMPT_TOKENS)


def _prompt(seq: int) -> str:
    if SHARED_PREFIX:
        # Same prefix every time: the second request onward should be a prefix
        # cache hit, and `orch_sticky_total` should move.
        return f"{_FILLER}\n\n{random.choice(QUESTIONS)}"
    return f"[{seq}] {_FILLER}\n\n{random.choice(QUESTIONS)}"


@events.test_start.add_listener
def _say_what_will_happen(environment, **_kwargs):
    """Print the arithmetic before the run, not after.

    A run that cannot reach the threshold is indistinguishable from a policy
    that does nothing, and the difference is knowable in advance.
    """
    resident = PROMPT_TOKENS + MAX_TOKENS
    need = int(0.90 * 217_000 / resident)
    print(f"  prompt ~{PROMPT_TOKENS} tokens + up to {MAX_TOKENS} generated "
          f"= ~{resident} resident per request")
    print(f"  to reach 0.90 KV on a whole-card 32B: ~{need} concurrent "
          f"(the engine runs at most 64 at once)")
    if need > 64:
        print(f"  WARNING: {need} > 64, so the KV threshold is UNREACHABLE here.")
        print("           raise LOAD_PROMPT_TOKENS, or lower SLICE_MIB on the fleet.")
    print(f"  prefix: {'shared — expect cache hits' if SHARED_PREFIX else 'distinct per request'}")


class ChatUser(HttpUser):
    wait_time = between(0.4, 1.2)
    host = os.environ.get("LOCUST_HOST", "http://127.0.0.1:8080")

    def on_start(self) -> None:
        self.tenant = f"locust-{id(self) % 10_000}"
        self.seq = 0

    def _chat(self, payload: dict, label: str) -> None:
        payload["tenant"] = getattr(self, "tenant", "locust")
        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            name=label,
            catch_response=True,
        ) as resp:
            # Three outcomes, reported as three different things, because the
            # whole point of the demo is that they are not the same event.
            #
            # 503 used to be marked a FAILURE, so admission control working
            # painted the UI red — the opposite of what it means. 429 was
            # marked a success, which hid a client quota behind a green bar.
            if resp.status_code == 200:
                resp.success()
            elif resp.status_code == 503:
                # The policy refused for capacity. This is the fleet behaving
                # correctly under pressure, so it is named rather than failed.
                resp.failure("503 shed — admission control refused (EXPECTED)")
            elif resp.status_code == 429:
                resp.failure("429 tenant quota — NOT capacity, raise TENANT_TOKENS_PER_MIN")
            elif resp.status_code == 400:
                resp.failure(f"400 guard — {resp.text[:120]}")
            else:
                resp.failure(f"{resp.status_code} {resp.text[:160]}")

    @task
    def text(self) -> None:
        self.seq += 1
        self._chat(
            {
                "model": "text",
                "messages": [{"role": "user", "content": _prompt(self.seq)}],
                "max_tokens": MAX_TOKENS,
            },
            "chat (text)",
        )
