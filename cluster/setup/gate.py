#!/usr/bin/env python3
"""Four capacity checks against a running engine. Run on the GPU box.

Two of them decide whether anything else is worth doing: one recomputes
kv_bytes_per_token from the model's own config.json, the other reads back what
vLLM actually reserved and checks the two agree. Every concurrency figure in
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


def serves(port: int) -> str | None:
    """Returns the served model id, so the KV check reads that model's config."""
    try:
        body = _get(f"http://localhost:{port}/v1/models")
        name = (json.loads(body).get("data") or [{}])[0].get("id", "?")
        check(True, "engine serves", name)
        return name
    except Exception as exc:
        check(False, "engine serves", f"{type(exc).__name__}: {exc}")
        return None


def metrics(port: int) -> None:
    try:
        m = _get(f"http://localhost:{port}/metrics")
    except Exception as exc:
        check(False, "KV-usage metric present", str(exc))
        return
    # Admission is a function of this metric. vLLM V1 renamed
    # gpu_cache_usage_perc -> kv_cache_usage_perc, so accept and report either.
    names = [n for n in ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc") if n in m]
    check(bool(names), "KV-usage metric present", names[0] if names else "neither name found")
    check("vllm:num_requests_waiting" in m, "num_requests_waiting present")


def _config_for(model_id: str | None) -> str | None:
    """The cached config.json for the model being served.

    Falling back to the most recent one silently checks the wrong model as soon
    as a second is cached, which is exactly when the number matters.
    """
    hub = os.path.expanduser("~/.cache/huggingface/hub")
    if model_id and "/" in model_id:
        owner, name = model_id.split("/", 1)
        exact = glob.glob(f"{hub}/models--{owner}--{name}/snapshots/*/config.json")
        if exact:
            return sorted(exact, key=os.path.getmtime)[-1]
    hits = glob.glob(f"{hub}/models--*/snapshots/*/config.json")
    return sorted(hits, key=os.path.getmtime)[-1] if hits else None


def kv_bytes_per_token(model_id: str | None = None) -> float | None:
    """KiB of KV per token from config.json. Every concurrency figure uses it."""
    path = _config_for(model_id)
    if not path:
        check(False, "KV bytes/token", "no config.json in the HF cache")
        return None
    cfg = json.load(open(path))
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
              "KV bytes/token", f"{detail} (expected {expected:.0f})")
    else:
        check(kib > 0, "KV bytes/token", detail)
    print(f"           one 16k sequence = {kib * 16384 / 1024 / 1024:.3f} GiB")
    return kib


def budget(log: str, budget_gib: float | None, util: float, predicted_kib: float | None,
           *, sliced: bool) -> None:
    """What the engine reserved, and whether its KiB/token agrees.

    Budget minus KV is weights + activations + CUDA graphs. Only sanity-checked,
    not predicted: overhead needs the quantised weight size, which config.json
    does not give. The agreement check is the one that carries evidence.
    """
    if not os.path.exists(log):
        check(False, "memory budget", f"no {log} — nothing to read")
        return
    text = open(log, errors="replace").read()

    kv = re.search(r"Available KV cache memory:\s*([\d.]+)\s*GiB", text)
    toks = re.search(r"GPU KV cache size:\s*([\d,]+)\s*tokens", text)
    concurrency = re.search(r"Maximum concurrency for ([\d,]+) tokens per request:\s*([\d.]+)x", text)
    if not kv:
        check(False, "memory budget", "no 'Available KV cache memory' line yet")
        return
    kv_gib = float(kv.group(1))

    if budget_gib is None:
        check(False, "memory budget", "could not read memory.total from nvidia-smi")
        return

    what = "slice" if sliced else "card"
    overhead_gib = budget_gib * util - kv_gib
    sane = 0 < overhead_gib < budget_gib * util
    detail = (f"{what} {budget_gib:.1f} GiB x {util:.2f} = {budget_gib * util:.2f}, "
              f"KV {kv_gib:.2f} -> overhead {overhead_gib:.2f} GiB")
    if EXPECT_OVERHEAD_GIB:
        expected = float(EXPECT_OVERHEAD_GIB)
        check(sane and abs(overhead_gib - expected) < expected * 0.25,
              "memory budget", f"{detail} (expected {expected:.2f})")
    else:
        check(sane, "memory budget", detail)

    # Arrived at independently, so agreement is real evidence.
    if toks:
        reported_kib = kv_gib * 1024**2 / int(toks.group(1).replace(",", ""))
        if predicted_kib:
            check(abs(reported_kib - predicted_kib) < predicted_kib * KV_TOLERANCE,
                  "engine agrees with config",
                  f"engine {reported_kib:.1f} vs config {predicted_kib:.1f} KiB/token")
        else:
            print(f"           engine reports {reported_kib:.1f} KiB/token")

    # vLLM's own concurrency figure. This is the number the whole capacity
    # argument rests on, so print it whether or not anything is asserted.
    if concurrency:
        print(f"           vLLM: {float(concurrency.group(2)):.1f} concurrent "
              f"{concurrency.group(1)}-token sequences per worker")
    if not sliced:
        # CUDA-graph capture scales with available memory, so a whole-card
        # overhead under-predicts a slice's pool. Measured: 18% out.
        print("           whole-card figure — do NOT extrapolate to a slice")


def main() -> int:
    ap = argparse.ArgumentParser(description="Capacity checks against a running vLLM")
    ap.add_argument("--port", type=int, default=8000, help="where that vLLM answers")
    ap.add_argument("--log", default=os.path.expanduser("~/dodona/engine.log"))
    ap.add_argument("--slice-mib", type=int, default=0,
                    help="budget against a HAMi slice of this size instead of the whole card")
    args = ap.parse_args()

    model_id = serves(args.port)
    metrics(args.port)
    predicted_kib = kv_bytes_per_token(model_id)
    sliced = args.slice_mib > 0
    budget_gib = args.slice_mib / 1024.0 if sliced else card_total_gib()
    budget(args.log, budget_gib, UTIL, predicted_kib, sliced=sliced)

    failed = _results.count(False)
    if failed:
        print(f"\n    {failed} of {len(_results)} checks failed.")
        print("    A KV or budget failure means the arithmetic is wrong, not the cluster —")
        print("    fix docs/project/01-capacity.md before renting more time.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
