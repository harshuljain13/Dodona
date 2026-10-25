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
profiling counters nvidia-smi cannot reach; this covers the three the
dashboards use, and says so rather than pretending to be the real exporter.

    python3 infra/local/gpu_exporter.py [--port 9400]

Stdlib only, so it needs nothing installed and cannot drift with a dependency.
"""
from __future__ import annotations

import argparse
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

#: What nvidia-smi is asked for, and the DCGM field each becomes. `gpu` is the
#: label dcgm-exporter uses for the index, so the dashboards' `by (gpu)`
#: aggregations work unchanged.
FIELDS = (
    ("utilization.gpu", "DCGM_FI_DEV_GPU_UTIL", "gauge", "GPU utilization (%)"),
    ("memory.used", "DCGM_FI_DEV_FB_USED", "gauge", "Framebuffer memory used (MiB)"),
    ("power.draw", "DCGM_FI_DEV_POWER_USAGE", "gauge", "Power draw (W)"),
    ("temperature.gpu", "DCGM_FI_DEV_GPU_TEMP", "gauge", "GPU temperature (C)"),
)


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


def render() -> str:
    """Prometheus text format: HELP and TYPE per family, then one line per GPU."""
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
            lines.append(f'{metric}{{gpu="{row["index"]}"}} {value}')
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        if self.path.rstrip("/") not in ("", "/metrics"):
            self.send_response(404)
            self.end_headers()
            return
        body = render().encode()
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
    args = parser.parse_args()
    print(f"gpu exporter on {args.host}:{args.port}/metrics  ({len(sample())} GPU(s))", flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
