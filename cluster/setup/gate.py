#!/usr/bin/env python3
"""Four capacity checks against a running engine. Run on the GPU box.

Two of them decide whether anything else is worth doing. 5c reads the model's
own config.json and recomputes kv_bytes_per_token; 5d reads back what vLLM
actually reserved and checks the two agree. Every concurrency figure in
docs/project/01-capacity.md is derived from those, so a failure here means the
arithmetic is wrong, not the cluster.

Exits non-zero if any check fails, so up.sh can stop on it.

    python3 cluster/setup/gate.py [--port 8000] [--log engine.log]

Optional, to assert a specific prediction rather than just report:

    EXPECT_KV_KIB=56 EXPECT_OVERHEAD_GIB=6.0 python3 cluster/setup/gate.py
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# Nothing about the card or model is hardcoded. Set either to assert a specific
# prediction instead of only reporting what was measured.
EXPECT_KV_KIB = os.environ.get("EXPECT_KV_KIB", "").strip()
EXPECT_OVERHEAD_GIB = os.environ.get("EXPECT_OVERHEAD_GIB", "").strip()

#: What the engine was told to take. Must match engine.sh's --gpu-memory-utilization.
UTIL = float(os.environ.get("UTIL", "0.90"))

#: Agreement tolerance between predicted and engine-reported KiB/token.
KV_TOLERANCE = 0.05


def card_total_gib() -> float | None:
    """Usable memory on GPU 0, read off the card rather than assumed."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        ).stdout.split()
        return int(out[0]) / 1024.0
    except Exception:
        return None

_PASS, _FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
_results: list[bool] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append(ok)
    print(f"    [{_PASS if ok else _FAIL}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def _get(url: str, timeout: float = 10.0) -> str:
    return urllib.request.urlopen(url, timeout=timeout).read().decode()


def serves(port: int) -> None:
    try:
        body = _get(f"http://localhost:{port}/v1/models")
        name = (json.loads(body).get("data") or [{}])[0].get("id", "?")
        check(True, "5a  engine serves", name)
    except Exception as exc:
        check(False, "5a  engine serves", f"{type(exc).__name__}: {exc}")


def metrics(port: int) -> None:
    try:
        m = _get(f"http://localhost:{port}/metrics")
    except Exception as exc:
        check(False, "5b  gpu_cache_usage_perc present", str(exc))
        return
    # Admission is a function of this metric. vLLM V1 renamed
    # gpu_cache_usage_perc -> kv_cache_usage_perc, so accept and report either.
    names = [n for n in ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc") if n in m]
    check(bool(names), "5b  KV-usage metric present", names[0] if names else "neither name found")
    check("vllm:num_requests_waiting" in m, "5b  num_requests_waiting present")


def kv_bytes_per_token() -> float | None:
    """KiB of KV per token from config.json. Every concurrency figure uses it."""
    hits = glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--*/snapshots/*/config.json"))
    if not hits:
        check(False, "5c  KV bytes/token", "no config.json in the HF cache")
        return None
    cfg = json.load(open(sorted(hits, key=os.path.getmtime)[-1]))
    layers = cfg["num_hidden_layers"]
    kv_heads = cfg["num_key_value_heads"]
    # Qwen3 declares head_dim explicitly and it is NOT hidden/heads. Deriving
    # it the usual way is wrong by 60% on that family.
    head_dim = cfg.get("head_dim") or cfg["hidden_size"] // cfg["num_attention_heads"]
    kib = 2 * layers * kv_heads * head_dim * 2 / 1024

    detail = f"layers={layers} kv_heads={kv_heads} head_dim={head_dim} -> {kib:.0f} KiB/token"
    if EXPECT_KV_KIB:
        expected = float(EXPECT_KV_KIB)
        check(abs(kib - expected) < expected * KV_TOLERANCE,
              "5c  KV bytes/token", f"{detail} (expected {expected:.0f})")
    else:
        check(kib > 0, "5c  KV bytes/token", detail)
    print(f"           one 16k sequence = {kib * 16384 / 1024 / 1024:.3f} GiB")
    return kib


def budget(log: str, card_gib: float | None, util: float, predicted_kib: float | None) -> None:
    """What the engine reserved, and whether its KiB/token agrees with 5c.

    Budget minus KV is weights + activations + CUDA graphs. Only sanity-checked,
    not predicted: overhead needs the quantised weight size, which config.json
    does not give. The agreement check is the one that carries evidence.
    """
    if not os.path.exists(log):
        check(False, "5d  memory budget", f"no {log} — started outside up.sh?")
        return
    text = open(log, errors="replace").read()

    kv = re.search(r"Available KV cache memory:\s*([\d.]+)\s*GiB", text)
    toks = re.search(r"GPU KV cache size:\s*([\d,]+)\s*tokens", text)
    if not kv:
        check(False, "5d  memory budget", "no 'Available KV cache memory' line yet")
        return
    kv_gib = float(kv.group(1))

    if card_gib is None:
        check(False, "5d  memory budget", "could not read memory.total from nvidia-smi")
        return

    overhead_gib = card_gib * util - kv_gib
    sane = 0 < overhead_gib < card_gib * util
    detail = (f"card {card_gib:.1f} GiB x {util:.2f} = {card_gib * util:.2f}, "
              f"KV {kv_gib:.2f} -> overhead {overhead_gib:.2f} GiB")
    if EXPECT_OVERHEAD_GIB:
        expected = float(EXPECT_OVERHEAD_GIB)
        check(sane and abs(overhead_gib - expected) < expected * 0.25,
              "5d  memory budget", f"{detail} (expected {expected:.2f})")
    else:
        check(sane, "5d  memory budget", detail)

    # Arrived at independently, so agreement is real evidence.
    if toks:
        reported_kib = kv_gib * 1024**2 / int(toks.group(1).replace(",", ""))
        if predicted_kib:
            check(abs(reported_kib - predicted_kib) < predicted_kib * KV_TOLERANCE,
                  "5d  engine agrees with 5c",
                  f"engine {reported_kib:.1f} vs config {predicted_kib:.1f} KiB/token")
        else:
            print(f"           engine reports {reported_kib:.1f} KiB/token")

    # Never extrapolate to a slice: CUDA-graph capture scales with available
    # memory, so read it off the pods with `grep "Maximum concurrency"` instead.
    print("           whole-card figure — do NOT extrapolate to a slice;")
    print("           read it off the running pods (see 01-capacity.md)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log", default=os.path.expanduser("~/dodona/engine.log"))
    args = ap.parse_args()

    serves(args.port)
    metrics(args.port)
    predicted_kib = kv_bytes_per_token()
    budget(args.log, card_total_gib(), UTIL, predicted_kib)

    failed = _results.count(False)
    if failed:
        print(f"\n    {failed} of {len(_results)} checks failed.")
        print("    5c or 5d failing means the arithmetic is wrong, not the cluster —")
        print("    fix docs/project/01-capacity.md before renting more time.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
