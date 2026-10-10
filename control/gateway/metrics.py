from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from gateway.types import Snapshot

SHED_REASONS = frozenset(
    {"tenant_tokens", "timeout_queue", "kv_free", "p99_spread", "no_eligible_pod"}
)

LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

STAGES = ("gateway", "pick", "local", "overflow", "e2e")

class Histogram:
    def __init__(self, buckets: tuple[float, ...] = LATENCY_BUCKETS) -> None:
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.inf = 0
        self.sum = 0.0
        self.count = 0

    def observe(self, seconds: float) -> None:
        value = max(0.0, float(seconds))
        self.sum += value
        self.count += 1
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1
                return
        self.inf += 1

    def render(self, name: str, labels: str = "", *, include_type: bool = True) -> list[str]:
        prefix = f"{labels}," if labels else ""
        lines = [f"# TYPE {name} histogram"] if include_type else []
        cumulative = 0
        for bound, n in zip(self.buckets, self.counts):
            cumulative += n
            lines.append(f'{name}_bucket{{{prefix}le="{bound}"}} {cumulative}')
        cumulative += self.inf
        lines.append(f'{name}_bucket{{{prefix}le="+Inf"}} {cumulative}')
        if labels:
            lines.append(f"{name}_sum{{{labels}}} {self.sum}")
            lines.append(f"{name}_count{{{labels}}} {self.count}")
        else:
            lines.append(f"{name}_sum {self.sum}")
            lines.append(f"{name}_count {self.count}")
        return lines

class Metrics:
    def __init__(self) -> None:
        self.requests_total = 0
        self.shed_total: dict[tuple[str, int], int] = defaultdict(int)
        self.pick_total = 0
        self.pick_unknown_snapshot_total = 0
        self.sticky_total = 0
        self.tokens_in_flight = {"prefill": 0, "decode": 0}
        self.kv_free_ratio = 0.0
        self.planner_desired = {"prefill": 0, "decode": 0}
        self.overflow_total = 0
        self.completed_total = 0
        self.kv_transfer_total = 0
        self.kv_transfer_tokens = 0
        self.kv_evict_total = 0
        self.place_total: dict[str, int] = defaultdict(int)
        self.latency: dict[str, Histogram] = {stage: Histogram() for stage in STAGES}
        self.replicas: dict[str, list[Snapshot]] = {"prefill": [], "decode": []}

    def inc_requests(self) -> None:
        self.requests_total += 1

    def inc_shed(self, reason: str, code: int) -> None:
        if reason not in SHED_REASONS:
            reason = "no_eligible_pod"
        self.shed_total[(reason, int(code))] += 1

    def inc_pick(self) -> None:
        self.pick_total += 1

    def inc_unknown_snapshot(self) -> None:
        self.pick_unknown_snapshot_total += 1

    def inc_sticky(self) -> None:
        self.sticky_total += 1

    def inc_place(self, capability: str) -> None:
        self.place_total[capability or "text"] += 1

    def set_in_flight(self, phase: str, n: int) -> None:
        self.tokens_in_flight[phase] = int(n)

    def set_kv_free(self, ratio: float) -> None:
        self.kv_free_ratio = float(ratio)

    def set_desired(self, pool: str, n: int) -> None:
        self.planner_desired[pool] = int(n)

    def inc_overflow(self) -> None:
        self.overflow_total += 1

    def inc_completed(self) -> None:
        self.completed_total += 1

    def inc_kv_transfer(self, tokens: int) -> None:
        self.kv_transfer_total += 1
        self.kv_transfer_tokens += int(tokens)

    def inc_kv_evict(self, n: int = 1) -> None:
        self.kv_evict_total += int(n)

    def observe_duration(self, stage: str, seconds: float) -> None:
        if stage not in self.latency:
            self.latency[stage] = Histogram()
        self.latency[stage].observe(seconds)

    def observe_replicas(self, pool: str, workers: Iterable[object]) -> None:
        snaps: list[Snapshot] = []
        for worker in workers:
            try:
                snap = worker.snapshot()
            except Exception:
                snap = Snapshot(pod_id=getattr(worker, "id", "unknown"), age_s=0.0, healthy=False)
            snaps.append(snap)
        key = "decode" if pool in ("decode", "vision") else "prefill"
        self.replicas[key] = snaps
        self.set_in_flight(key, sum(int(s.tokens_in_flight or 0) for s in snaps))
        frees = [float(s.kv_free_ratio) for s in snaps if s.kv_free_ratio is not None]
        if frees:
            self.set_kv_free(min(frees))

    def refresh_from_router(self, router: object) -> None:
        prefill = getattr(router, "prefill", ())
        decode = getattr(router, "decode", ())
        self.observe_replicas("prefill", prefill)
        self.observe_replicas("decode", decode)

    def profile_summary(self) -> list[str]:
        lines = [
            f"{'stage':<10} {'count':>7} {'sum_s':>10} {'avg_ms':>10}",
            f"{'-'*10} {'-'*7} {'-'*10} {'-'*10}",
        ]
        for stage in STAGES:
            hist = self.latency.get(stage) or Histogram()
            avg_ms = (hist.sum / hist.count * 1000.0) if hist.count else 0.0
            lines.append(f"{stage:<10} {hist.count:>7} {hist.sum:>10.3f} {avg_ms:>10.1f}")
        lines.append("")
        lines.append(
            f"requests={self.requests_total} completed={self.completed_total} "
            f"picks={self.pick_total} sticky={self.sticky_total} overflow={self.overflow_total}"
        )
        return lines

    def reset(self) -> None:
        self.__init__()
        from router.trace import TRACES

        TRACES.reset()

    def render(self) -> str:
        lines = [
            "# TYPE orch_requests_total counter",
            f"orch_requests_total {self.requests_total}",
            "# TYPE orch_shed_total counter",
        ]
        if not self.shed_total:
            lines.append('orch_shed_total{reason="no_eligible_pod",code="503"} 0')
        for (reason, code), n in sorted(self.shed_total.items()):
            lines.append(f'orch_shed_total{{reason="{reason}",code="{code}"}} {n}')
        lines += [
            "# TYPE orch_pick_total counter",
            f"orch_pick_total {self.pick_total}",
            "# TYPE orch_pick_unknown_snapshot_total counter",
            f"orch_pick_unknown_snapshot_total {self.pick_unknown_snapshot_total}",
            "# TYPE orch_sticky_total counter",
            f"orch_sticky_total {self.sticky_total}",
            "# TYPE orch_tokens_in_flight gauge",
            f'orch_tokens_in_flight{{phase="prefill"}} {self.tokens_in_flight["prefill"]}',
            f'orch_tokens_in_flight{{phase="decode"}} {self.tokens_in_flight["decode"]}',
            "# TYPE orch_kv_free_ratio gauge",
            f"orch_kv_free_ratio {self.kv_free_ratio}",
            "# TYPE orch_planner_desired_replicas gauge",
            f'orch_planner_desired_replicas{{pool="prefill"}} {self.planner_desired["prefill"]}',
            f'orch_planner_desired_replicas{{pool="decode"}} {self.planner_desired["decode"]}',
            "# TYPE orch_overflow_total counter",
            f"orch_overflow_total {self.overflow_total}",
            "# TYPE orch_completed_total counter",
            f"orch_completed_total {self.completed_total}",
            "# TYPE orch_kv_transfer_total counter",
            f"orch_kv_transfer_total {self.kv_transfer_total}",
            "# TYPE orch_kv_transfer_tokens counter",
            f"orch_kv_transfer_tokens {self.kv_transfer_tokens}",
            "# TYPE orch_kv_evict_total counter",
            f"orch_kv_evict_total {self.kv_evict_total}",
            "# TYPE orch_place_total counter",
        ]
        if not self.place_total:
            lines.append('orch_place_total{capability="text"} 0')
        for cap, n in sorted(self.place_total.items()):
            lines.append(f'orch_place_total{{capability="{cap}"}} {n}')
        first = True
        for stage in STAGES:
            hist = self.latency.get(stage) or Histogram()
            lines += hist.render(
                "orch_request_duration_seconds",
                f'stage="{stage}"',
                include_type=first,
            )
            first = False
        lines += [
            "# TYPE orch_replica_healthy gauge",
            "# TYPE orch_replica_saturating gauge",
            "# TYPE orch_replica_kv_free_ratio gauge",
            "# TYPE orch_replica_tokens_in_flight gauge",
            "# TYPE orch_replica_waiting gauge",
            "# TYPE orch_replica_running gauge",
            "# TYPE orch_replica_queue_depth gauge",
            "# TYPE orch_replica_active_requests gauge",
        ]
        any_replica = False
        for pool, snaps in (("prefill", self.replicas["prefill"]), ("decode", self.replicas["decode"])):
            for snap in snaps:
                any_replica = True
                labels = f'pool="{pool}",pod="{snap.pod_id}"'
                lines.append(f'orch_replica_healthy{{{labels}}} {1 if snap.healthy else 0}')
                lines.append(f'orch_replica_saturating{{{labels}}} {1 if snap.saturating else 0}')
                if snap.kv_free_ratio is not None:
                    lines.append(f'orch_replica_kv_free_ratio{{{labels}}} {snap.kv_free_ratio}')
                if snap.tokens_in_flight is not None:
                    lines.append(f'orch_replica_tokens_in_flight{{{labels}}} {snap.tokens_in_flight}')
                if snap.waiting is not None:
                    lines.append(f'orch_replica_waiting{{{labels}}} {snap.waiting}')
                if snap.running is not None:
                    lines.append(f'orch_replica_running{{{labels}}} {snap.running}')
                if snap.queue_depth is not None:
                    lines.append(f'orch_replica_queue_depth{{{labels}}} {snap.queue_depth}')
                if snap.active_requests is not None:
                    lines.append(f'orch_replica_active_requests{{{labels}}} {snap.active_requests}')
        if not any_replica:
            lines.append('orch_replica_healthy{pool="prefill",pod="none"} 0')
        return "\n".join(lines) + "\n"

METRICS = Metrics()
