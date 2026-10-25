from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.metrics import METRICS

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "infra" / "metrics"
K8S = ROOT / "infra" / "cluster" / "k8s"

#: Model names reach the guardrail allowlist through the environment, and
#: guardrails.py falls back to a 3B name when they are unset — which is what
#: these tests assert against. A developer who has run `source .env`, as
#: PREREQUISITES tells them to, would otherwise see three failures about model
#: names that have nothing to do with their change.
MODEL_ENV = ("TEXT_MODEL", "VISION_MODEL", "LOCAL_MODEL")

#: Pointing at a real fleet. Stripped so an in-process test uses FakeWorker
#: rather than dialling a port that happens to be in someone's .env. ORCH_URL
#: is here because the Lab posts to it and reports "orch unreachable" when it
#: is set and nothing answers.
FLEET_ENV = ("PREFILL_URLS", "DECODE_URLS", "OVERFLOW_BASE_URL", "OVERFLOW_API_KEY", "ORCH_URL")


def fleet_is_answering() -> bool:
    """Is there actually a vLLM behind PREFILL_URLS right now?

    `PREFILL_URLS` being set means someone has a .env, not that a fleet is up —
    and `source .env` is the first thing PREREQUISITES tells you to do. A live
    test that fails because nothing is listening reports a broken repo when the
    honest answer is "no fleet here".
    """
    import socket
    import urllib.parse

    raw = os.environ.get("PREFILL_URLS", "").split(",")[0].strip()
    if not raw:
        return False
    parsed = urllib.parse.urlparse(raw)
    if not parsed.hostname:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _reset_metrics(request: pytest.FixtureRequest) -> None:
    os.environ.pop("TRACE_PATH", None)
    os.environ.pop("MOONCAKE_URL", None)
    os.environ.pop("KV_BACKEND", None)
    os.environ.pop("LAB_SPLIT", None)
    saved: dict[str, str | None] = {}
    # Always: a test's verdict must not depend on whether a shell sourced .env.
    for key in MODEL_ENV:
        saved[key] = os.environ.pop(key, None)
    if request.node.get_closest_marker("lambda") is None:
        for key in FLEET_ENV:
            saved[key] = os.environ.pop(key, None)
    METRICS.reset()
    yield
    METRICS.reset()
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value
