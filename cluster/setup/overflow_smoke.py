from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from router.overflow import FakeOverflow, Overflow
from router.router import Router
from gateway.admission import Gateway
from router.kvbus import KVBus
from gateway.metrics import METRICS
from router.pools import FakeWorker
from router.trace import TRACES
from gateway.types import Request

ROOT = Path(__file__).resolve().parents[1]

def _req(name: str) -> Request:
    return Request(
        id=name,
        arrival_t=0.0,
        priority=1,
        prompt_tokens=16,
        max_new_tokens=8,
        prefix_hash=None,
        timeout_s=60.0,
        tenant="lab",
    )

def _print_event(title: str, resp) -> None:
    last = TRACES.events[-1] if TRACES.events else {}
    print(f"== {title} ==")
    print(
        f"via={resp.via} happened={last.get('happened')} status={resp.status} "
        f"local_status={resp.local_status} local_ms={last.get('local_ms')} "
        f"overflow_ms={last.get('overflow_ms')} total_ms={last.get('total_ms')}"
    )
    print(json.dumps(last, sort_keys=True))

def hop_local() -> None:

    METRICS.reset()
    bus = KVBus()
    prefill = [FakeWorker("p0", bus=bus)]
    decode = [FakeWorker("d0", bus=bus)]
    gw = Gateway(Router(prefill, decode), tokens_per_min=10_000_000)
    resp = Overflow(local=gw, backup=FakeOverflow()).send(_req("local-ping"))
    _print_event("hop 1 local (fake-gpu)", resp)
    if resp.status != 200 or resp.via != "local":
        raise SystemExit(f"hop 1 expected via=local status=200, got via={resp.via} status={resp.status}")

def hop_overflow() -> None:

    METRICS.reset()
    bus = KVBus()
    prefill = [FakeWorker("p0", bus=bus)]
    decode = [FakeWorker("d0", bus=bus)]
    for w in prefill + decode:
        w.saturating = True
    gw = Gateway(Router(prefill, decode), tokens_per_min=10_000_000)
    resp = Overflow(local=gw).send(_req("overflow-ping"))
    _print_event("hop 2 overflow (local 503)", resp)
    if resp.via != "overflow" or resp.local_status not in (503, 529):
        raise SystemExit(
            f"hop 2 expected via=overflow after local 503/529, got via={resp.via} local_status={resp.local_status}"
        )
    if resp.status != 200:
        raise SystemExit(f"hop 2 backup failed status={resp.status} error={resp.error}")

def main() -> None:
    if not os.environ.get("OVERFLOW_BASE_URL", "").strip():
        print("OVERFLOW_BASE_URL unset — skip")
        raise SystemExit(0)
    os.environ.setdefault("TRACE_PATH", str(ROOT / "traces" / "requests.jsonl"))
    print("Overflow: local first. Backup generate catches a fleet 503/529. Traces stamp via + happened + ms.")
    hop_local()
    hop_overflow()
    path = os.environ["TRACE_PATH"]
    print(f"== {path} ==")
    text = Path(path).read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines[-2:]:
        print(ln)
    overflow_lines = [ln for ln in lines if '"via": "overflow"' in ln]
    if not overflow_lines:
        raise SystemExit("trace is missing via=overflow")
    print("OVERFLOW SMOKE PASS")

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"OVERFLOW SMOKE FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
