#!/usr/bin/env python3
"""Read the cluster's metrics and say what they mean.

Not a dump. Every number is printed with the thing it tells you, and anything
that looks wrong is called out with what to do about it. Run it on the GPU box:

    python3 infra/cluster/setup/report.py                 # what is the cluster doing
    python3 infra/cluster/setup/report.py --dashboards    # why panels are empty
    python3 infra/cluster/setup/report.py --json out.json

The one question it exists to answer, at the bottom of every run:

    was anything actually scarce, and could an admission policy have fired?

A latency number without that beside it cannot distinguish a policy that worked
from one that never had anything to decide about.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
import urllib.request

G, Y, R, D, B = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m"
X = "\033[0m"

#: From docs/project/01-capacity.md, measured on the running fleet.
KV_PER_TOKEN_KIB = 56.0
KV_PER_SLICE_GIB = 4.27
SEQ_PER_REPLICA = 4.88
MAX_MODEL_LEN = 16384
THRESHOLD = 0.90


def _default_report_path() -> str:
    """Where a bare --json lands. Reports are output, so they go under infra/."""
    root = Path(__file__).resolve().parents[3]      # repo root
    stamp = subprocess.run(["date", "-u", "+%Y%m%dT%H%M%SZ"],
                           capture_output=True, text=True).stdout.strip()
    return str(root / "benchmarking" / "reports" / f"cluster-{stamp}.json")


def sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=60).stdout.strip()
    except Exception:
        return ""


def promql(q: str) -> list[tuple[dict, float]]:
    """Query Prometheus through kubectl exec — no port-forward needed."""
    raw = sh('kubectl -n monitoring exec deploy/prometheus-server -c prometheus-server -- '
             f'wget -qO- "http://localhost:9090/api/v1/query?query={urllib.parse.quote(q)}" 2>/dev/null')
    try:
        return [(r["metric"], float(r["value"][1]))
                for r in json.loads(raw)["data"]["result"]]
    except Exception:
        return []


def head(t: str) -> None:
    print(f"\n{B}{t}{X}\n{'─' * len(t)}")


def note(level: str, msg: str) -> None:
    c = {"ok": G, "warn": Y, "bad": R, "info": D}[level]
    tag = {"ok": "ok  ", "warn": "WARN", "bad": "BAD ", "info": "    "}[level]
    print(f"  {c}{tag}{X}  {msg}")


# ── sections ────────────────────────────────────────────────────────────────

def fleet(out: dict) -> None:
    head("Fleet")
    pods = sh("kubectl get pods -o json")
    try:
        items = json.loads(pods)["items"]
    except Exception:
        note("bad", "kubectl returned nothing — are you on the box?")
        return
    vllm = [p for p in items if p["metadata"]["name"].startswith("vllm-")]
    ready = [p for p in vllm if all(c.get("ready") for c in
                                    p["status"].get("containerStatuses", []))]
    out["pods_total"], out["pods_ready"] = len(vllm), len(ready)
    for p in sorted(vllm, key=lambda p: p["metadata"]["name"]):
        n = p["metadata"]["name"]
        lim = p["spec"]["containers"][0].get("resources", {}).get("limits", {})
        r = all(c.get("ready") for c in p["status"].get("containerStatuses", []))
        note("ok" if r else "warn",
             f"{n:<32} {p['status']['phase']:<9} "
             f"gpumem={lim.get('nvidia.com/gpumem','?')} cores={lim.get('nvidia.com/gpucores','?')}")

    smi = sh("nvidia-smi --query-compute-apps=used_memory --format=csv,noheader,nounits")
    mibs = [int(x) for x in smi.split() if x.isdigit()]
    if mibs:
        note("info", f"nvidia-smi: {len(mibs)} process(es), "
                     f"{sum(mibs):,} MiB total ({sum(mibs)/1024:.1f} GiB of 40)")
        if len(mibs) != len(vllm):
            note("warn", f"{len(mibs)} GPU processes but {len(vllm)} vLLM pods — "
                         "a pod may be starting, or something else holds the card")
        if max(mibs) > 12288:
            note("bad", f"a process holds {max(mibs)} MiB, above the 12288 MiB slice — "
                        "HAMi is not enforcing the cap and the run is void")


def engine(out: dict) -> None:
    head("Engine — what each worker is doing")
    rows = {m.get("pod", "?"): v for m, v in promql("vllm:kv_cache_usage_perc")}
    run = {m.get("pod", "?"): v for m, v in promql("vllm:num_requests_running")}
    wait = {m.get("pod", "?"): v for m, v in promql("vllm:num_requests_waiting")}
    if not rows:
        note("bad", "no vllm: metrics in Prometheus — check the scrape annotations")
        return
    out["kv"] = rows
    for pod in sorted(rows):
        kv, r, w = rows[pod], run.get(pod, 0), wait.get(pod, 0)
        seq = kv * KV_PER_SLICE_GIB / (KV_PER_TOKEN_KIB * MAX_MODEL_LEN / 1024 / 1024)
        lvl = "bad" if kv >= THRESHOLD else "warn" if kv >= 0.7 else "ok"
        note(lvl, f"{pod:<32} kv={kv:5.1%}  running={r:>4.0f}  waiting={w:>4.0f}   "
                  f"≈{seq:.1f} of {SEQ_PER_REPLICA:.1f} full-length sequences")
    pre = promql("sum(increase(vllm:num_preemptions_total[15m]))")
    if pre and pre[0][1] > 0:
        note("bad", f"{pre[0][1]:.0f} preemptions in 15m — the engine ran out of blocks for a "
                    "sequence it had already started. Admission let through more than the fleet holds")
    else:
        note("ok", "no preemptions — nothing was evicted mid-generation")

    hits = promql("sum(increase(vllm:prefix_cache_hits_total[15m]))")
    qs = promql("sum(increase(vllm:prefix_cache_queries_total[15m]))")
    if hits and qs and qs[0][1] > 0:
        rate = hits[0][1] / qs[0][1]
        out["prefix_hit_rate"] = rate
        note("ok" if rate > 0.7 else "warn",
             f"prefix cache {rate:.1%} hit rate (P4 predicts >70%) — "
             f"the ~1k shared prompt being reused")


def gpu() -> None:
    head("GPU")
    for q, label, fmt in [("DCGM_FI_DEV_GPU_UTIL", "utilisation", "{:.0f}%"),
                          ("DCGM_FI_DEV_FB_USED", "framebuffer used", "{:,.0f} MiB"),
                          ("DCGM_FI_DEV_POWER_USAGE", "power", "{:.0f} W")]:
        r = promql(q)
        if r:
            note("info", f"{label:<18} " + fmt.format(r[0][1]))
        else:
            note("warn", f"{label:<18} no data — DCGM exporter not reaching Prometheus")


def saturation(out: dict) -> None:
    head("Was anything scarce?")
    peak = promql(f"max_over_time(vllm:kv_cache_usage_perc[30m])")
    if not peak:
        note("warn", "no history — has any load run in the last 30 minutes?")
        return
    hi = max(v for _, v in peak)
    out["kv_peak_30m"] = hi
    print(f"  peak kv_cache_usage_perc over 30m: {B}{hi:.1%}{X}  (threshold {THRESHOLD:.0%})")
    if hi >= THRESHOLD:
        note("ok", "the signal CROSSED the threshold — a kv_threshold policy could have fired. "
                   "Its decisions are measurable")
    else:
        need = THRESHOLD / hi if hi else 99
        note("warn", f"the signal never reached {THRESHOLD:.0%}. A kv_threshold policy "
                     f"would have been {B}INERT{X}{Y} — installed, running on every request, "
                     f"unable to change any outcome")
        note("info", f"roughly {need:.1f}x more concurrent load would be needed, "
                     "or longer holds: more max_tokens, no gap between turns")
        note("info", "this is a result, not a failure — but better known now than after the load tests")


def dashboards() -> None:
    head("Why Grafana panels are empty")
    present = {}
    for pre in ("vllm:", "orch_", "DCGM_FI", "kube_", "node_", "container_", "mooncake"):
        raw = sh('kubectl -n monitoring exec deploy/prometheus-server -c prometheus-server -- '
                 'wget -qO- "http://localhost:9090/api/v1/label/__name__/values" 2>/dev/null')
        present[pre] = len(re.findall(f'"{pre}', raw))
    for pre, n in present.items():
        if n:
            note("ok", f"{pre:<12} {n:>4} metric names")
        else:
            why = {"orch_": "the gateway is not deployed — it emits every orch_* metric",
                   "mooncake": "Mooncake is not deployed; its image does not exist yet"}
            note("warn", f"{pre:<12} {'none':>4}         ← {why.get(pre, 'not installed')}")
    print()
    note("info", "The dodona dashboards are built almost entirely on orch_* — 16 of their")
    note("info", "top 20 queried metrics. Those panels are CORRECTLY empty: they measure a")
    note("info", "gateway that does not exist. Nothing is misconfigured.")
    note("info", "What works today: the vLLM panels, DCGM GPU, and the kube/node/cAdvisor ones.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboards", action="store_true", help="only the empty-panel explanation")
    ap.add_argument("--json", nargs="?", const=_default_report_path(),
                    default="", help="also write the numbers here "
                         "(bare flag writes benchmarking/reports/cluster-<date>.json)")
    a = ap.parse_args()
    out: dict = {}

    if a.dashboards:
        dashboards()
        return 0

    fleet(out)
    engine(out)
    gpu()
    saturation(out)
    dashboards()

    if a.json:
        json.dump(out, open(a.json, "w"), indent=2)
        print(f"\n  numbers -> {a.json}")
    return 0


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
