from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.metrics import METRICS

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "infra" / "metrics"
K8S = ROOT / "infra" / "cluster" / "k8s"

@pytest.fixture(autouse=True)
def _reset_metrics(request: pytest.FixtureRequest) -> None:
    os.environ.pop("TRACE_PATH", None)
    os.environ.pop("MOONCAKE_URL", None)
    os.environ.pop("KV_BACKEND", None)
    os.environ.pop("LAB_SPLIT", None)
    saved: dict[str, str | None] = {}
    if request.node.get_closest_marker("lambda") is None:
        for key in ("PREFILL_URLS", "DECODE_URLS", "OVERFLOW_BASE_URL", "OVERFLOW_API_KEY"):
            saved[key] = os.environ.pop(key, None)
    METRICS.reset()
    yield
    METRICS.reset()
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value
