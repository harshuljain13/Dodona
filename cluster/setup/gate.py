#!/usr/bin/env python3
"""The stage-0 gate: four checks, run on the GPU box.

Two of them decide whether anything else is worth doing. 5c reads the model's
own config.json and recomputes kv_bytes_per_token; 5d reads back what vLLM
actually reserved. Every concurrency figure in docs/project/01-capacity.md is
derived from those two, so a failure here means the arithmetic is wrong, not
the cluster.

Exits non-zero if any check fails, so `make gate` and up.sh can stop on it.

    python3 cluster/setup/gate.py [--port 8000] [--log engine.log]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request

#: Predicted in docs/project/01-capacity.md. 2 (K,V) x 28 layers x 4 kv_heads
#: x 128 head_dim x 2 B = 57,344 B. Written here so a drift shows up as a
#: failure rather than as a silently different number.
PREDICTED_KV_KIB = 56.0
PREDICTED_WEIGHTS_GIB = 5.20

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
    # Admission is a function of this metric. Without it there is nothing to
    # decide on, and stages 4 onward have no input.
    check("gpu_cache_usage_perc" in m, "5b  gpu_cache_usage_perc present")


def kv_bytes_per_token() -> None:
    """Prediction P6. The one that invalidates the rest if it is wrong."""
    hits = glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--*/snapshots/*/config.json"))
    if not hits:
        check(False, "5c  kv_bytes_per_token (P6)", "no config.json in the HF cache")
        return
    cfg = json.load(open(sorted(hits, key=os.path.getmtime)[-1]))
    layers = cfg["num_hidden_layers"]
    kv_heads = cfg["num_key_value_heads"]
    # Qwen3 declares head_dim explicitly and it is NOT hidden/heads. Deriving
    # it the usual way is wrong by 60% on that family.
    head_dim = cfg.get("head_dim") or cfg["hidden_size"] // cfg["num_attention_heads"]
    kib = 2 * layers * kv_heads * head_dim * 2 / 1024
    check(
        abs(kib - PREDICTED_KV_KIB) < PREDICTED_KV_KIB * 0.05,
        "5c  kv_bytes_per_token (P6)",
        f"layers={layers} kv_heads={kv_heads} head_dim={head_dim} "
        f"-> {kib:.0f} KiB/token (predicted {PREDICTED_KV_KIB:.0f})",
    )
    if kib:
        print(f"           one 16k sequence = {kib * 16384 / 1024 / 1024:.3f} GiB")


def weights(log: str) -> None:
    """What vLLM actually reserved, against the 5.20 GiB estimate."""
    if not os.path.exists(log):
        check(False, "5d  weights on the card", f"no {log} — is the engine running under up.sh?")
        return
    text = open(log, errors="replace").read()
    lines = [ln.strip() for ln in text.splitlines()
             if re.search(r"KV cache|model weights|GPU blocks|[Mm]arlin|quantiz", ln)]
    m = re.search(r"model weights(?: take| takes)?\s*([\d.]+)\s*GiB", text)
    if m:
        got = float(m.group(1))
        check(abs(got - PREDICTED_WEIGHTS_GIB) < PREDICTED_WEIGHTS_GIB * 0.15,
              "5d  weights on the card",
              f"{got:.2f} GiB (predicted {PREDICTED_WEIGHTS_GIB:.2f})")
    else:
        check(False, "5d  weights on the card", "no 'model weights' line in the log yet")
    for ln in lines[-6:]:
        print(f"           {ln[:120]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log", default=os.path.expanduser("~/dodona/engine.log"))
    args = ap.parse_args()

    serves(args.port)
    metrics(args.port)
    kv_bytes_per_token()
    weights(args.log)

    failed = _results.count(False)
    if failed:
        print(f"\n    {failed} of {len(_results)} checks failed.")
        print("    5c or 5d failing means the arithmetic is wrong, not the cluster —")
        print("    fix docs/project/01-capacity.md before renting more time.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
