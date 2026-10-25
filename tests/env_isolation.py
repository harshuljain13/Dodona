"""Environment variables that must not reach a test or a harness run.

One list, two readers: `conftest.py` strips these from pytest, and the Makefile
unsets them around the harness loops. The harnesses are separate processes, so
a pytest fixture cannot reach them — and keeping a second copy of this list in
the Makefile is how the two drift until a behaviour fails for a reason nobody
can see.

Why any of it is needed: `source .env` is the first thing PREREQUISITES tells
you to do, so `ssh $BOX` works. After that, a shell carries a model name and a
set of pool URLs into every process it starts.

Deliberately NOT stdlib-free theatre — this module imports nothing, so the
Makefile can read it with a bare `python -c` that does not need pytest, vllm or
anything else installed.
"""
from __future__ import annotations

#: Model names reach the guardrail allowlist through the environment, and
#: edge/guardrails.py falls back to a 3B name when they are unset — which is
#: what the guardrail tests and the `local-qwen-alias` behaviour assert
#: against. A .env naming a 32B silently rewrites the allowlist.
MODEL_ENV = ("TEXT_MODEL", "VISION_MODEL", "LOCAL_MODEL")

#: Pointing at a real fleet. Stripped so an in-process run uses FakeWorker
#: rather than dialling a port that happens to be in someone's .env. ORCH_URL
#: is here because the Lab posts to it and reports "orch unreachable" when it
#: is set and nothing answers.
FLEET_ENV = (
    "PREFILL_URLS",
    "DECODE_URLS",
    "OVERFLOW_BASE_URL",
    "OVERFLOW_API_KEY",
    "ORCH_URL",
)

#: Knobs the Lab and the KV bus read directly. `LAB_SPLIT=capability` in a .env
#: is what made `unhealthy-decode-scales` fail: the behaviour asserts on the
#: default split and the harness silently ran a different one.
LAB_ENV = ("TRACE_PATH", "MOONCAKE_URL", "KV_BACKEND", "LAB_SPLIT")

#: Everything a hermetic run should start without.
ALL_ENV = MODEL_ENV + FLEET_ENV + LAB_ENV
