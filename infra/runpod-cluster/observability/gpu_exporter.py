#!/usr/bin/env python3
"""GPU telemetry on :9400, under the metric names the dashboards already query.

The Kubernetes path runs `nvcr.io/nvidia/k8s/dcgm-exporter` as a DaemonSet. A
rented pod cannot run containers, so there was no GPU telemetry at all on the
process path — `cluster` is the dashboard that shows whether the card is
*busy*, and half of what this project demonstrates is pressure on a card.

So: same numbers, different collector. The values come from `nvidia-smi`, which
reads the same driver DCGM reads, and they are published under
`DCGM_FI_DEV_*` because that is what `dashboards.py` queries:

    dashboards.py:513   DCGM_FI_DEV_GPU_UTIL
    dashboards.py:514   DCGM_FI_DEV_FB_USED
    dashboards.py:515   DCGM_FI_DEV_POWER_USAGE

Not a reimplementation of DCGM. DCGM reports hundreds of fields including
profiling counters nvidia-smi cannot reach; this covers the ten the dashboards
use, and says so rather than pretending to be the real exporter.

Each series also carries `pod` and `phase`, which real DCGM does not emit: it
has no idea what is running on the card. workers.sh pins one worker per GPU and
passes the mapping in, so a hardware panel can say "the prefill card is at 90%"
instead of "GPU 0 is at 90%".

    python3 infra/runpod-cluster/observability/gpu_exporter.py [--port 9400]

Stdlib only, so it needs nothing installed and cannot drift with a dependency.
"""
from __future__ import annotations

import argparse
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

#: What nvidia-smi is asked for, and the DCGM field each becomes. `gpu` is the
#: label dcgm-exporter uses for the index, so the dashboards' `by (gpu)`
#: aggregations work unchanged. Every name here is a real DCGM field, so a box
#: that later runs the actual exporter serves the same dashboards.
FIELDS = (
    ("utilization.gpu", "DCGM_FI_DEV_GPU_UTIL", "gauge", "GPU utilization (%)"),
    ("utilization.memory", "DCGM_FI_DEV_MEM_COPY_UTIL", "gauge",
     "Memory bandwidth utilization (%)"),
    ("memory.used", "DCGM_FI_DEV_FB_USED", "gauge", "Framebuffer memory used (MiB)"),
    ("memory.free", "DCGM_FI_DEV_FB_FREE", "gauge", "Framebuffer memory free (MiB)"),
    ("memory.total", "DCGM_FI_DEV_FB_TOTAL", "gauge", "Framebuffer memory total (MiB)"),
    ("power.draw", "DCGM_FI_DEV_POWER_USAGE", "gauge", "Power draw (W)"),
    ("enforced.power.limit", "DCGM_FI_DEV_ENFORCED_POWER_LIMIT", "gauge",
     "Enforced power limit (W)"),
    ("temperature.gpu", "DCGM_FI_DEV_GPU_TEMP", "gauge", "GPU temperature (C)"),
    ("clocks.sm", "DCGM_FI_DEV_SM_CLOCK", "gauge", "SM clock (MHz)"),
    ("clocks.mem", "DCGM_FI_DEV_MEMORY_CLOCK", "gauge", "Memory clock (MHz)"),
)

#: Which worker sits on which card. nvidia-smi knows the index and nothing else,
#: so without this a hardware panel can only say "GPU 1" — and the question
#: being asked of these dashboards is always "is the *prefill* card saturated?".
#: workers.sh pins one worker per GPU in this order, and passes the mapping in.
DEFAULT_ROLES = "0=prefill,1=decode-0,2=decode-1"


def parse_roles(spec: str) -> dict[str, str]:
    """`"0=prefill,1=decode-0"` -> `{"0": "prefill", "1": "decode-0"}`.

    Malformed entries are skipped rather than fatal: a GPU with no mapping still
    gets its metrics, just labelled `pod="gpu-N"`. Losing a label is a worse
    dashboard; losing the exporter is no dashboard at all.
    """
    roles: dict[str, str] = {}
    for item in spec.split(","):
        index, _, pod = item.partition("=")
        if _ and index.strip() and pod.strip():
            roles[index.strip()] = pod.strip()
    return roles


def sample() -> list[dict[str, str]]:
    """One row per GPU, or an empty list if nvidia-smi is unavailable.

    Empty rather than raising: a scrape that returns no series is read as "this
    exporter has nothing to say", which is true. An exporter that 500s on a
    machine without a GPU would make Prometheus mark the target down and hide
    the fact that everything else is fine.
    """
    query = ",".join(["index"] + [f for f, _, _, _ in FIELDS])
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    rows = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(FIELDS) + 1:
            continue
        rows.append(dict(zip(["index"] + [f for f, _, _, _ in FIELDS], parts)))
    return rows


def render(roles: dict[str, str] | None = None) -> str:
    """Prometheus text format: HELP and TYPE per family, then one line per GPU."""
    roles = roles if roles is not None else parse_roles(
        os.environ.get("GPU_ROLES", DEFAULT_ROLES))
    lines: list[str] = []
    rows = sample()
    for field, metric, kind, help_text in FIELDS:
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} {kind}")
        for row in rows:
            raw = row.get(field, "")
            try:
                value = float(raw)
            except ValueError:
                # "[N/A]" is what nvidia-smi prints for an unsupported field —
                # power draw is absent on some cards. Skipping the series is
                # honest; publishing 0 would read as an idle GPU.
                continue
            index = row["index"]
            pod = roles.get(index, f"gpu-{index}")
            # `phase` matches the label Prometheus puts on the vLLM targets, so
            # a hardware panel and an engine panel can be read side by side.
            # `both` for an aggregated engine, which really does serve both
            # phases — calling it "decode" to keep a panel company is how a
            # graph starts lying. `unknown` only for a card with no worker
            # mapped to it at all.
            if pod.startswith("prefill"):
                phase = "prefill"
            elif pod.startswith("decode"):
                phase = "decode"
            elif pod.startswith("gpu-"):
                phase = "unknown"
            else:
                phase = "both"
            lines.append(
                f'{metric}{{gpu="{index}",pod="{pod}",phase="{phase}"}} {value}')
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    #: Set by main() from --roles. A class attribute because BaseHTTPRequestHandler
    #: is instantiated per request and takes no constructor arguments of ours.
    roles: dict[str, str] | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        if self.path.rstrip("/") not in ("", "/metrics"):
            self.send_response(404)
            self.end_headers()
            return
        body = render(self.roles).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        """Silent. A scrape every five seconds would bury the fleet's own log."""
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9400)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--roles", default=os.environ.get("GPU_ROLES", DEFAULT_ROLES),
                        help="index=pod pairs, e.g. 0=prefill,1=decode-0 — the label "
                             "that lets a hardware panel name the worker on the card")
    args = parser.parse_args()
    roles = parse_roles(args.roles)
    Handler.roles = roles
    print(f"gpu exporter on {args.host}:{args.port}/metrics  "
          f"({len(sample())} GPU(s), roles: {roles or 'none'})", flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
