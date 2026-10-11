from __future__ import annotations

import os
import time
from typing import Callable
import urllib.error

from gateway.admitperf_gate import Gate
from gateway.queue import enqueue
from gateway.metrics import METRICS
from router.router import Router
from gateway.types import Request, Response, Shed, SliceOOM

WINDOW_S = 60.0

#: Per-tenant token budget per minute. 0 disables it.
#:
#: Was a hardcoded 10_000, which is less than TWO requests at the length an admission
#: experiment needs: a prompt at a 7B's KV crossover on an A100-40 is ~8.2k tokens, so the
#: second arrival exhausted the budget and 28 of 32 requests came back 429 tenant_tokens.
#: A rate limit is client-attributable and says nothing about capacity — but in a report
#: it is a refusal, and it looked exactly like the KV policy working.
DEFAULT_TOKENS_PER_MIN = int(os.environ.get("TENANT_TOKENS_PER_MIN") or 10_000)

class Gateway:

    def __init__(self, router: Router, tokens_per_min: int | None = None,
                 *, clock: Callable[[], float] | None = None) -> None:
        self.router = router
        self.tokens_per_min = (
            DEFAULT_TOKENS_PER_MIN if tokens_per_min is None else tokens_per_min
        )
        #: The rate limiter's own clock. Monotonic so a clock adjustment cannot
        #: hand a tenant a free window or freeze one shut.
        self._clock = clock or time.monotonic
        self._window: dict[str, tuple[float, int]] = {}
        # The AdmitPerf policy, from ADMISSION_* in the environment. Off when
        # ADMISSION_POLICY is unset, leaving this gateway's own behaviour unchanged.
        self.gate = Gate()

    def refresh_metrics(self) -> None:
        METRICS.refresh_from_router(self.router)

    def handle(self, req: Request) -> Response:
        t0 = time.perf_counter()
        METRICS.inc_requests()
        if self._over_cap(req):
            METRICS.inc_shed("tenant_tokens", 429)
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Shed(
                status=429,
                error="rate_limit_error",
                reason="tenant_tokens",
                message="tenant tokens/min cap",
            ).to_response()

        # AdmitPerf decides whether to serve at all. After the tenant cap, because a
        # quota refusal is the client's fault and says nothing about capacity; before
        # `place`, because choosing among replicas is a different question.
        decision = self.gate.decide(self.router, request_id=req.id)
        if decision is not None and not decision.admitted:
            METRICS.inc_shed(decision.reason, decision.status)
            self.refresh_metrics()
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Shed(
                status=decision.status,
                error="server_is_overloaded" if decision.status == 503 else "rate_limit_error",
                reason=decision.reason,
                message=f"admitperf: {decision.reason}",
            ).to_response()

        result = self.router.place(req)
        if isinstance(result, Shed):
            METRICS.inc_shed(result.reason, result.status)
            self.refresh_metrics()
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return result.to_response()
        try:
            hop, completion = enqueue(result, req)
        except SliceOOM:
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Response(
                status=503,
                error="slice_oom",
                body={"error": {"type": "slice_oom", "message": "gpumem slice exceeded"}},
            )
        except (OSError, TimeoutError, urllib.error.URLError):
            METRICS.inc_shed("no_eligible_pod", 503)
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Shed(
                status=503,
                error="server_is_overloaded",
                reason="no_eligible_pod",
                message="worker unreachable",
            ).to_response()
        if req.evict_after and hop:
            bus = getattr(result.prefill, "bus", None) or getattr(result.decode, "bus", None)
            if bus is not None and hasattr(bus, "evict"):
                bus.evict(hop.get("src") or result.prefill.id, req.prefix_hash)
                bus.evict(hop.get("dst") or result.decode.id, req.prefix_hash)
        METRICS.inc_completed()
        METRICS.inc_place(req.capability or "text")
        # The outcome, joined to its decision by request_id. Without it the log holds
        # verdicts and no consequences, so a report can say what the policy DID but not
        # what it BOUGHT.
        self.gate.outcome(
            req.id,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            status=200,
            served=True,
        )
        self.refresh_metrics()
        METRICS.observe_duration("gateway", time.perf_counter() - t0)
        body: dict = {"id": req.id}
        if hop:
            body["kv_hop"] = hop
        if isinstance(completion, dict):
            for key in ("choices", "model", "usage"):
                if key in completion:
                    body[key] = completion[key]
            if completion.get("id"):
                body["completion_id"] = completion["id"]
        return Response(status=200, handoff=result, body=body)

    def _over_cap(self, req: Request) -> bool:
        """Tokens per minute, per tenant, on a window the SERVER advances.

        This used to window on `req.arrival_t`, which `request_from_openai`
        defaults to 0.0 whenever the client omits it — and every real client
        omits it. So `arrival_t - start` was `0.0 - 0.0` forever, the window
        never rolled over, and `used` accumulated for the life of the process:
        "2,000,000 tokens per minute" was really 2,000,000 tokens ever. The
        gateway then 429'd every request for the rest of its uptime, which in a
        capacity sweep looks exactly like the cluster refusing load it could not
        handle.

        A rate limiter cannot take its clock from the thing it is limiting.
        `arrival_t` is still honoured when given, because the behaviour harness
        drives synthetic arrivals to test the window itself.
        """
        now = req.arrival_t if req.arrival_t else self._clock()
        cost = int(req.prompt_tokens) + int(req.max_new_tokens)
        start, used = self._window.get(req.tenant, (now, 0))
        if now - start >= WINDOW_S:
            start, used = now, 0
        if used + cost > self.tokens_per_min:
            return True
        self._window[req.tenant] = (start, used + cost)
        return False
