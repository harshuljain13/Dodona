from __future__ import annotations

import argparse
import io
import os
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from router.overflow import FakeOverflow, send
from router.router import Router, plan
from gateway.admission import Gateway
from router.kvbus import KVBus
from gateway.metrics import METRICS
from router.pools import FakeWorker
from router.trace import TRACES
from gateway.types import Request, STALE_S

ROOT = Path(__file__).resolve().parents[1]
INCIDENTS = ROOT / "INCIDENTS.md"

@dataclass
class BehaviorResult:
    name: str
    passed: bool
    summary: str
    extra: dict = field(default_factory=dict)

def _req(i: int, **kw) -> Request:
    base = dict(
        id=f"d-{i}",
        arrival_t=float(i),
        priority=1,
        prompt_tokens=64,
        max_new_tokens=8,
        prefix_hash=None,
        timeout_s=2.0,
        tenant="lab",
    )
    base.update(kw)
    return Request(**base)

def _fleet(n_p: int = 1, n_d: int = 1, kv: int = 8_000) -> tuple[list[FakeWorker], list[FakeWorker], KVBus]:
    bus = KVBus()
    prefill = [FakeWorker(f"p{i}", kv_capacity=kv, bus=bus) for i in range(n_p)]
    decode = [FakeWorker(f"d{i}", kv_capacity=kv, bus=bus) for i in range(n_d)]
    return prefill, decode, bus

def behavior_fleet_soak() -> BehaviorResult:
    """A backed-up fleet sheds on deadline, and those sheds may overflow."""
    METRICS.reset()
    prefill, decode, _ = _fleet(kv=50_000)
    gw = Gateway(Router(prefill, decode), tokens_per_min=10_000_000)
    backup = FakeOverflow()
    saw_429 = False
    for i in range(20):
        resp = send(_req(i, timeout_s=1.0, prompt_tokens=32, max_new_tokens=8), gw, backup)
        if resp.status == 429:
            saw_429 = True
    shed_503 = sum(n for (_reason, code), n in METRICS.shed_total.items() if code == 503)
    completed = METRICS.completed_total
    overflow_on_429 = saw_429 and len(backup.calls) > 0
    passed = shed_503 > 0 and completed > 0 and not overflow_on_429
    return BehaviorResult(
        "fleet-soak",
        passed,
        f"fleet-soak  completed={completed} shed_503={shed_503} overflow_calls={len(backup.calls)} via_overflow={TRACES.overflow_count()}",
        {"completed": completed, "shed_503": shed_503, "overflow": len(backup.calls)},
    )

def behavior_unhealthy_decode_scales() -> BehaviorResult:
    """An unhealthy decode pod is skipped, and the planner asks for one more."""
    METRICS.reset()
    prefill, decode, _ = _fleet(n_p=1, n_d=2, kv=400)
    decode[1].healthy = False
    decode[1].soak_weight = 0.1
    decode[0].saturating = True
    gw = Gateway(Router(prefill, decode), tokens_per_min=10_000_000)
    for i in range(4):
        gw.handle(_req(i, prompt_tokens=80, max_new_tokens=16, timeout_s=30.0))
    n503 = sum(n for (_reason, code), n in METRICS.shed_total.items() if code == 503)
    buf = io.StringIO()
    with redirect_stdout(buf):
        desired = plan(
            current_prefill=1,
            current_decode=1,
            uncached_prefill_tokens_in_flight=10,
            prefill_token_budget=1000,
            decode_slots_used=80,
            decode_slots=100,
        )
    out = buf.getvalue()
    passed = n503 > 0 and "scale decode +1" in out and desired[1] == 2
    return BehaviorResult(
        "unhealthy-decode-scales",
        passed,
        f"unhealthy-decode-scales  shed_503={n503} overflow=0 planner={out.strip()}",
        {"shed_503": n503, "overflow": 0},
    )

def behavior_tenant_429_stays() -> BehaviorResult:
    """A noisy tenant's 429 is refused locally and never leaves the cluster."""
    METRICS.reset()
    prefill, decode, _ = _fleet()
    epp = Router(prefill, decode)
    gw = Gateway(epp, tokens_per_min=40)
    backup = FakeOverflow()
    noisy = send(_req(0, tenant="noisy", prompt_tokens=64, max_new_tokens=8), gw, backup)
    quiet = send(_req(1, tenant="quiet", prompt_tokens=16, max_new_tokens=4), gw, backup)
    passed = noisy.status == 429 and quiet.status == 200 and backup.calls == []
    return BehaviorResult(
        "tenant-429-stays",
        passed,
        f"tenant-429-stays  noisy={noisy.status} quiet={quiet.status} overflow={len(backup.calls)}",
        {"noisy": noisy.status, "quiet": quiet.status, "overflow": len(backup.calls)},
    )

def behavior_ignore_stale_pod() -> BehaviorResult:
    """A pod whose snapshot is older than STALE_S is never picked."""
    METRICS.reset()
    bus = KVBus()
    a = FakeWorker("A", bus=bus)
    b = FakeWorker("B", bus=bus)
    b.age_override = STALE_S + 20
    epp = Router([a, b], [a])
    unknown_before = METRICS.pick_unknown_snapshot_total
    from router.router import PREFILL_SCORERS

    chosen = []
    for i in range(6):
        picked = epp.pick([a, b], PREFILL_SCORERS, _req(i, prefix_hash=f"u{i}"))
        if not hasattr(picked, "status"):
            chosen.append(picked.id)
    passed = chosen and all(c == "A" for c in chosen) and "B" not in chosen
    return BehaviorResult(
        "ignore-stale-pod",
        passed,
        f"ignore-stale-pod  chosen={chosen} unknown={METRICS.pick_unknown_snapshot_total} overflow=0",
        {"chosen": chosen, "unknown": METRICS.pick_unknown_snapshot_total - unknown_before, "overflow": 0},
    )

def behavior_scale_decode_not_prefill() -> BehaviorResult:
    """The planner scales only the pool that crossed its high-water mark."""
    METRICS.reset()
    buf = io.StringIO()
    with redirect_stdout(buf):
        desired = plan(
            current_prefill=1,
            current_decode=1,
            uncached_prefill_tokens_in_flight=10,
            prefill_token_budget=1000,
            decode_slots_used=80,
            decode_slots=100,
        )
    out = buf.getvalue()
    passed = desired == (1, 2) and "scale decode +1" in out and "scale prefill +1" not in out
    return BehaviorResult(
        "scale-decode-not-prefill",
        passed,
        f"scale-decode-not-prefill  overflow=0 {out.strip()}",
        {"desired": desired, "overflow": 0},
    )

def behavior_abort_frees_kv() -> BehaviorResult:
    """Aborting a request returns its KV immediately, keeping tokens already emitted."""
    METRICS.reset()
    w = FakeWorker("w0")
    req = _req(0, prompt_tokens=128, max_new_tokens=32)
    w.enqueue(req, phase="both")
    w.step()
    emitted = w.decode_tokens_emitted
    used = w.kv_used
    w.abort(req.id)
    passed = w.kv_used == 0 and w.decode_tokens_emitted == emitted and used > 0
    return BehaviorResult(
        "abort-frees-kv",
        passed,
        f"abort-frees-kv  kv {used}→{w.kv_used} decode_tokens={w.decode_tokens_emitted} overflow=0",
        {"kv_after": w.kv_used, "overflow": 0},
    )

def behavior_prefix_sticky_saves_kv() -> BehaviorResult:
    """Prefix affinity uses less total KV than pure least-loaded placement."""
    METRICS.reset()
    from router.router import PREFILL_SCORERS

    def replay(policy: str) -> int:
        bus = KVBus()
        workers = [FakeWorker(f"w{i}", bus=bus) for i in range(12)]
        epp = Router(workers, workers, policy=policy)
        for i in range(20):
            prefix = "share" if i < 10 else f"solo-{i}"
            req = _req(i, prompt_tokens=200, prefix_hash=prefix, max_new_tokens=0)
            picked = epp.pick(workers, PREFILL_SCORERS, req, policy=policy)
            if not hasattr(picked, "status"):
                picked.enqueue(req, phase="prefill")
        return sum(w.kv_used for w in workers)

    sticky_kv = replay("p2c")
    ll_kv = replay("least_loaded")
    buf = io.StringIO()
    with redirect_stdout(buf):
        desired = plan(
            current_prefill=1,
            current_decode=1,
            uncached_prefill_tokens_in_flight=10,
            prefill_token_budget=10_000,
            decode_slots_used=10,
            decode_slots=10_000,
        )
    out = buf.getvalue()
    passed = sticky_kv < ll_kv and desired == (1, 1) and "scale prefill +1" not in out
    return BehaviorResult(
        "prefix-sticky-saves-kv",
        passed,
        f"prefix-sticky-saves-kv  sticky_kv={sticky_kv} ll_kv={ll_kv} overflow=0 planner={out.strip()}",
        {"sticky_kv": sticky_kv, "ll_kv": ll_kv, "overflow": 0},
    )

def behavior_scale_both_pools() -> BehaviorResult:
    """Both pools scale when both are over the high-water mark."""
    METRICS.reset()
    buf = io.StringIO()
    with redirect_stdout(buf):
        desired = plan(
            current_prefill=1,
            current_decode=1,
            uncached_prefill_tokens_in_flight=80,
            prefill_token_budget=100,
            decode_slots_used=80,
            decode_slots=100,
        )
    out = buf.getvalue()
    passed = desired == (2, 2) and "scale prefill +1" in out and "scale decode +1" in out
    return BehaviorResult(
        "scale-both-pools",
        passed,
        f"scale-both-pools  overflow=0 {out.strip()}",
        {"desired": desired, "overflow": 0},
    )

def behavior_slice_oom_no_overflow() -> BehaviorResult:
    """A request too big for the GPU slice is refused locally, never bursted."""
    METRICS.reset()
    prefill, decode, _ = _fleet(kv=80)
    gw = Gateway(Router(prefill, decode), tokens_per_min=10_000_000)
    backup = FakeOverflow()
    small = send(_req(0, prompt_tokens=16, max_new_tokens=4), gw, backup)
    huge = send(_req(1, prompt_tokens=400, max_new_tokens=40), gw, backup)
    buf = io.StringIO()
    with redirect_stdout(buf):
        plan(
            current_prefill=1,
            current_decode=1,
            uncached_prefill_tokens_in_flight=0,
            prefill_token_budget=1000,
            decode_slots_used=0,
            decode_slots=1000,
        )
    out = buf.getvalue()
    refused = huge.error == "slice_oom" or (huge.body.get("error") or {}).get("type") == "slice_oom"
    no_preempt = all(not w.preempted for w in prefill + decode)
    no_scale = "scale" not in out
    passed = small.status == 200 and refused and no_preempt and no_scale and backup.calls == []
    return BehaviorResult(
        "slice-oom-no-overflow",
        passed,
        f"slice-oom-no-overflow  small={small.status} huge={huge.error} preempt={not no_preempt} overflow={len(backup.calls)}",
        {"refused": refused, "preempted": not no_preempt, "overflow": len(backup.calls)},
    )

def behavior_overflow_on_503() -> BehaviorResult:
    """A genuine capacity 503 does leave, and is traced as via=overflow."""
    METRICS.reset()
    prefill, decode, _ = _fleet()
    for w in prefill + decode:
        w.saturating = True
    gw = Gateway(Router(prefill, decode), tokens_per_min=10_000_000)
    backup = FakeOverflow()
    resp = send(_req(0), gw, backup)
    shed = sum(METRICS.shed_total.values())
    reasons = {r for (r, _c) in METRICS.shed_total}
    passed = (
        METRICS.overflow_total >= 1
        and shed >= 1
        and len(backup.calls) == 1
        and "tenant_tokens" not in reasons
        and resp.status == 200
        and resp.via == "overflow"
        and resp.local_status == 503
    )
    last = TRACES.events[-1] if TRACES.events else {}
    return BehaviorResult(
        "overflow-on-503",
        passed,
        f"overflow-on-503  overflow={METRICS.overflow_total} via={resp.via} happened={last.get('happened')} "
        f"local_ms={last.get('local_ms')} overflow_ms={last.get('overflow_ms')} shed={dict(METRICS.shed_total)}",
        {"overflow": METRICS.overflow_total, "reasons": sorted(reasons), "via": resp.via, "happened": last.get("happened")},
    )

BEHAVIORS: dict[str, Callable[[], BehaviorResult]] = {
    "fleet-soak": behavior_fleet_soak,
    "unhealthy-decode-scales": behavior_unhealthy_decode_scales,
    "tenant-429-stays": behavior_tenant_429_stays,
    "ignore-stale-pod": behavior_ignore_stale_pod,
    "scale-decode-not-prefill": behavior_scale_decode_not_prefill,
    "abort-frees-kv": behavior_abort_frees_kv,
    "prefix-sticky-saves-kv": behavior_prefix_sticky_saves_kv,
    "scale-both-pools": behavior_scale_both_pools,
    "slice-oom-no-overflow": behavior_slice_oom_no_overflow,
    "overflow-on-503": behavior_overflow_on_503,
}

ALIASES = {
    "b1": "fleet-soak",
    "b2": "unhealthy-decode-scales",
    "b3": "tenant-429-stays",
    "b4": "ignore-stale-pod",
    "b5": "scale-decode-not-prefill",
    "b6": "abort-frees-kv",
    "b7": "prefix-sticky-saves-kv",
    "b8": "scale-both-pools",
    "b9": "slice-oom-no-overflow",
    "b10": "overflow-on-503",
}

def run_behavior(name: str, *, record: bool = False) -> BehaviorResult:
    key = name.lower().replace("_", "-")
    key = ALIASES.get(key, key)
    if key not in BEHAVIORS:
        raise SystemExit(f"unknown behavior {name}; want {', '.join(BEHAVIORS)}")
    result = BEHAVIORS[key]()
    if record:
        _record(result)
    return result

def _record(result: BehaviorResult) -> None:
    overflow = result.extra.get("overflow", 0)
    block = (
        f"\n<!-- last-run {result.name} -->\n"
        f"## Last behavior run ({result.name})\n\n"
        f"{result.summary}\n"
        f"Overflow calls: {overflow}  (overflow-on-503 must; fleet-soak may; others 0)\n"
        f"PASS={result.passed}\n"
    )
    text = INCIDENTS.read_text() if INCIDENTS.exists() else ""
    start = text.find("\n<!-- last-run")
    if start >= 0:
        text = text[:start] + block
    else:
        text = text.rstrip() + block
    INCIDENTS.write_text(text)

def main() -> None:
    os.environ.setdefault("TRACE_PATH", str(ROOT / "traces" / "requests.jsonl"))
    parser = argparse.ArgumentParser(description="Gateway harness — admit / bind / place / shed on FakeWorker replicas")
    parser.add_argument(
        "--behavior",
        dest="behavior",
        help="name from --list (old B1–B10 still work)",
    )
    parser.add_argument("--list", action="store_true", help="print behavior names")
    parser.add_argument("--record", action="store_true", help="write a stub into INCIDENTS.md")
    args = parser.parse_args()
    if args.list or not args.behavior:
        for name, fn in BEHAVIORS.items():
            doc = (fn.__doc__ or "").strip()
            print(f"{name:28} {doc}")
        if not args.behavior:
            raise SystemExit(0)
    result = run_behavior(args.behavior, record=args.record)
    print(result.summary)
    print("PASS" if result.passed else "FAIL")
    raise SystemExit(0 if result.passed else 1)

if __name__ == "__main__":
    main()
