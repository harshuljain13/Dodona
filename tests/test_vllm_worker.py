from __future__ import annotations

import os

import pytest

from router.pools import VLLMWorker
from gateway.types import Request

pytestmark = getattr(pytest.mark, "lambda")

@pytest.mark.skipif(not os.environ.get("PREFILL_URLS"), reason="PREFILL_URLS not set")
def test_vllm_short_prompt_snapshot() -> None:
    url = os.environ["PREFILL_URLS"].split(",")[0].strip()
    worker = VLLMWorker("lambda-0", url)
    worker.enqueue(
        Request(
            id="lambda-ping",
            arrival_t=0.0,
            priority=1,
            prompt_tokens=8,
            max_new_tokens=4,
            prefix_hash=None,
            timeout_s=60.0,
            tenant="lab",
        )
    )
    snap = worker.snapshot()
    assert snap.kv_free_ratio is not None
    assert 0 < snap.kv_free_ratio <= 1
    assert snap.waiting is not None and snap.waiting < float("inf")
    assert snap.running is not None and snap.running < float("inf")
