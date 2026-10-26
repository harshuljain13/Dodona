from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.metrics import METRICS

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "infra" / "metrics"

# One list, shared with the Makefile's harness loops — see env_isolation.py for
# why a pytest fixture is not enough.
from env_isolation import FLEET_ENV, LAB_ENV, MODEL_ENV


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
    for key in LAB_ENV:
        os.environ.pop(key, None)
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
