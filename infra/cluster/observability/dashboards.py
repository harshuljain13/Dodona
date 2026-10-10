from __future__ import annotations

import json
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}

METRIC_NAMES = {
    "overview": (
        "orch_requests_total",
        "orch_completed_total",
        "orch_shed_total",
        "orch_overflow_total",
        "orch_pick_total",
        "orch_kv_free_ratio",
        # orch_tokens_in_flight is NOT here: it is an instantaneous gauge and a
        # request lives ~90ms against a 15s scrape, so it reads zero even as a
        # 5-minute max. The overview shows token throughput by pool instead,
        # which is a rate over counters and moves whenever traffic does.
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
        "orch_request_duration_seconds",
        "orch_kv_transfer_total",
        "orch_kv_evict_total",
        "orch_pick_unknown_snapshot_total",
    ),
    "gateway": (
        "orch_requests_total",
        "orch_completed_total",
        "orch_shed_total",
        "orch_place_total",
        "orch_request_duration_seconds",
    ),
    "router": (
        "orch_pick_total",
        "orch_pick_unknown_snapshot_total",
        "orch_sticky_total",
        "orch_overflow_total",
        "orch_planner_desired_replicas",
        "orch_kv_transfer_total",
        "orch_kv_transfer_tokens",
        "orch_kv_evict_total",
        "orch_tokens_in_flight",
    ),
    "replicas": (
        "orch_replica_healthy",
        "orch_replica_saturating",
        "orch_replica_kv_free_ratio",
        "orch_replica_tokens_in_flight",
        "orch_replica_waiting",
        "orch_replica_running",
        "orch_replica_queue_depth",
        "orch_planner_desired_replicas",
        "kube_deployment_spec_replicas",
        "kube_deployment_status_replicas",
        "kube_pod_status_phase",
        "kube_pod_container_status_restarts_total",
    ),
    "vllm": (
        "vllm:gpu_cache_usage_perc",
        "vllm:kv_cache_usage_perc",
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
        "vllm:time_to_first_token_seconds",
        "vllm:inter_token_latency_seconds",
        "vllm:e2e_request_latency_seconds",
        "vllm:prefix_cache_hits_total",
        "vllm:prefix_cache_queries_total",
        "vllm:num_preemptions_total",
        "vllm:generation_tokens_total",
        "vllm:prompt_tokens_total",
    ),
    "keda": (
        "keda_scaler_metrics_value",
        "keda_scaler_active",
        "kube_deployment_spec_replicas",
        "kube_deployment_status_replicas",
        "orch_tokens_in_flight",
        "orch_planner_desired_replicas",
    ),
    "hami": (
        "GPUDeviceMemoryAllocated",
        "GPUDeviceCoreAllocated",
        "vGPU_device_memory_usage_in_bytes",
        "hami_gpu_memory_allocated_bytes",
        "hami_vgpu_memory_used_bytes",
        "kube_pod_container_resource_limits",
        "nvidia.com/gpumem",
        "nvidia.com/gpucores",
    ),
    "mooncake": (
        "vllm:prompt_tokens_by_source_total",
        "vllm:external_prefix_cache_queries_total",
        "vllm:external_prefix_cache_hits_total",
        "orch_kv_transfer_total",
        "orch_kv_transfer_tokens",
        "orch_kv_evict_total",
    ),
    "cluster": (
        "node_cpu_seconds_total",
        "node_memory_MemAvailable_bytes",
        "kube_node_status_condition",
        "kube_pod_status_phase",
        "kube_pod_container_status_restarts_total",
        "container_cpu_usage_seconds_total",
        "container_memory_working_set_bytes",
        "DCGM_FI_DEV_GPU_UTIL",
        "DCGM_FI_DEV_FB_USED",
        "DCGM_FI_DEV_POWER_USAGE",
    ),
    "errors": (
        "orch_requests_total",
        "orch_completed_total",
        "orch_shed_total",
        "orch_overflow_total",
        "orch_place_total",
        "orch_request_duration_seconds",
    ),
}

def _target(expr: str, legend: str = "", ref: str = "A") -> dict:
    target: dict = {"datasource": PROM, "expr": expr, "refId": ref}
    if legend:
        target["legendFormat"] = legend
    return target

def _panel(
    pid: int,
    title: str,
    expr: str,
    *,
    legend: str = "",
    kind: str = "timeseries",
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    extra: list[tuple[str, str]] | None = None,
    unit: str = "",
) -> dict:
    targets = [_target(expr, legend, "A")]
    for i, (ex, leg) in enumerate(extra or []):
        targets.append(_target(ex, leg, chr(ord("B") + i)))
    defaults: dict = {"custom": {"drawStyle": "line", "fillOpacity": 10, "lineWidth": 1}}
    if unit:
        defaults["unit"] = unit
    panel = {
        "id": pid,
        "type": kind,
        "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": PROM,
        "targets": targets,
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}},
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }
    if kind == "stat":
        panel["options"] = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
            "textMode": "auto",
        }
    return panel

def _dash(uid: str, title: str, panels: list[dict], tags: list[str]) -> dict:
    return {
        "uid": uid,
        "title": title,
        "tags": tags,
        "schemaVersion": 39,
        "version": 2,
        "timezone": "browser",
        "refresh": "5s",
        "time": {"from": "now-1h", "to": "now"},
        "templating": {"list": []},
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "links": [],
        "liveNow": True,
        "panels": panels,
        "style": "dark",
    }

def overview() -> dict:
    # EVERY query aggregates. orch-serve restarts on each `make cluster`, and a
    # new pod means a new series — nine deploys left nine orch_kv_free_ratio
    # lines, nine identical "picks" legends, and nine prefill/decode pairs. An
    # unaggregated gateway metric draws one line per pod generation, forever.
    return _dash(
        "dodona-overview",
        "Dodona / Overview (metrics.py)",
        [
            _panel(1, "Requests / s", "sum(rate(orch_requests_total[5m]))", x=0, y=0, w=6, h=5, kind="stat"),
            _panel(2, "Completed / s", "sum(rate(orch_completed_total[5m]))", x=6, y=0, w=6, h=5, kind="stat"),
            _panel(3, "Shed / s", "sum(rate(orch_shed_total[5m]))", x=12, y=0, w=6, h=5, kind="stat"),
            _panel(4, "Overflow / s", "sum(rate(orch_overflow_total[5m]))", x=18, y=0, w=6, h=5, kind="stat"),

            _panel(5, "KV free, tightest replica", "min(orch_kv_free_ratio)",
                   legend="min free", x=0, y=5, w=8, h=8),

            # orch_tokens_in_flight is an instantaneous gauge, and a request lives
            # ~90ms against a 15s scrape — it reads zero even as a 5-minute max.
            # Token throughput is a rate over counters, so it is non-zero whenever
            # traffic flows, and it shows the two pools doing different jobs.
            _panel(6, "Work by pool",
                   'sum(rate(vllm:prompt_tokens_total{pod=~"vllm-prefill.*"}[5m]))',
                   legend="prefill: prompt tok/s", x=8, y=5, w=8, h=8,
                   extra=[('sum(rate(vllm:prompt_tokens_total{pod=~"vllm-decode.*"}[5m]))',
                           "decode: prompt tok/s"),
                          ('sum(rate(vllm:generation_tokens_total{pod=~"vllm-decode.*"}[5m]))',
                           "decode: generated tok/s")]),

            # Per hour, not per second: on a quiet cluster a rate rounds to zero
            # and both lines sit on the axis, which is how "picks vs sheds" came
            # to look like it had no sheds in it.
            _panel(7, "Picks vs sheds (per hour)", "sum(increase(orch_pick_total[1h]))",
                   legend="picks", x=16, y=5, w=8, h=8,
                   extra=[("sum(increase(orch_shed_total[1h]))", "sheds")]),

            _panel(8, "p95 latency by stage",
                   'histogram_quantile(0.95, sum by (le, stage) '
                   '(rate(orch_request_duration_seconds_bucket[5m])))',
                   legend="{{stage}}", x=0, y=13, w=12, h=9),

            _panel(9, "KV transfer vs evict (per hour)",
                   "sum(increase(orch_kv_transfer_total[1h]))", legend="hops recorded",
                   x=12, y=13, w=8, h=9,
                   extra=[("sum(increase(orch_kv_evict_total[1h]))", "evicts")]),

            # Placements made with no usable snapshot are effectively random,
            # whatever the routing policy claims. Nothing else here shows it.
            _panel(10, "Blind placements (per hour)",
                   "sum(increase(orch_pick_unknown_snapshot_total[1h]))",
                   legend="no snapshot", x=20, y=13, w=4, h=9),
        ],
        ["dodona", "metrics.py", "overview"],
    )

def gateway() -> dict:
    return _dash(
        "dodona-gateway",
        "Dodona / Gateway + admission",
        [
            _panel(1, "Admit rate", "sum(rate(orch_requests_total[5m]))", x=0, y=0, w=8, h=6, kind="stat"),
            _panel(2, "Completed", "sum(rate(orch_completed_total[5m]))", x=8, y=0, w=8, h=6, kind="stat"),
            _panel(3, "Sheds by reason", "sum by (reason, code) (rate(orch_shed_total[5m]))",
                   legend="{{reason}} {{code}}", x=16, y=0, w=8, h=6),
            _panel(4, "Place by capability", "sum by (capability) (rate(orch_place_total[5m]))",
                   legend="{{capability}}", x=0, y=6, w=12, h=8),
            _panel(
                5,
                "Gateway p50 / p95 / p99",
                'histogram_quantile(0.95, sum by (le) (rate(orch_request_duration_seconds_bucket{stage="gateway"}[5m])))',
                legend="p95",
                x=12,
                y=6,
                w=12,
                h=8,
            ),
            _panel(
                6,
                "Stage latency heatmap (count)",
                "sum by (stage) (rate(orch_request_duration_seconds_count[5m]))",
                legend="{{stage}}",
                x=0,
                y=14,
                w=24,
                h=8,
            ),
        ],
        ["dodona", "gateway"],
    )

def router() -> dict:
    return _dash(
        "dodona-router",
        "Dodona / Router",
        [
            _panel(1, "Picks", "sum(increase(orch_pick_total[1h]))", x=0, y=0, w=6, h=5, kind="stat"),
            _panel(2, "Sticky hits", "sum(increase(orch_sticky_total[1h]))", x=6, y=0, w=6, h=5, kind="stat"),
            _panel(3, "Unknown snapshots", "sum(increase(orch_pick_unknown_snapshot_total[1h]))", x=12, y=0, w=6, h=5, kind="stat"),
            _panel(4, "Overflow", "sum(increase(orch_overflow_total[1h]))", x=18, y=0, w=6, h=5, kind="stat"),
            _panel(5, "Planner desired replicas", "max by (pool) (orch_planner_desired_replicas)", legend="{{pool}}", x=0, y=5, w=12, h=8),
            _panel(6, "Tokens in flight by phase", "sum by (phase) (orch_tokens_in_flight)", legend="{{phase}}", x=12, y=5, w=12, h=8),
            _panel(7, "KV transfer tokens", "sum(rate(orch_kv_transfer_tokens[5m]))", x=0, y=13, w=8, h=8),
            _panel(8, "KV transfers", "sum(rate(orch_kv_transfer_total[5m]))", x=8, y=13, w=8, h=8),
            _panel(9, "KV evicts", "sum(rate(orch_kv_evict_total[5m]))", x=16, y=13, w=8, h=8),
            _panel(
                10,
                "Pick latency p95",
                'histogram_quantile(0.95, sum by (le) (rate(orch_request_duration_seconds_bucket{stage="pick"}[5m])))',
                x=0,
                y=21,
                w=12,
                h=8,
            ),
            _panel(
                11,
                "Overflow vs local p95",
                'histogram_quantile(0.95, sum by (le, stage) (rate(orch_request_duration_seconds_bucket{stage=~"local|overflow|e2e"}[5m])))',
                legend="{{stage}}",
                x=12,
                y=21,
                w=12,
                h=8,
            ),
        ],
        ["dodona", "router"],
    )

def replicas() -> dict:
    lab = 'deployment=~"vllm-.*|orch-serve"'
    pod = 'pod=~"vllm-.*|orch-serve.*"'
    return _dash(
        "dodona-replicas",
        "Dodona / Pods and replicas",
        [
            _panel(1, "Desired replicas (kube)", f"kube_deployment_spec_replicas{{{lab}}}", legend="{{deployment}} spec", x=0, y=0, w=12, h=8),
            _panel(2, "Ready replicas (kube)", f"kube_deployment_status_replicas{{{lab}}}", legend="{{deployment}} ready", x=12, y=0, w=12, h=8),
            _panel(3, "Planner desired", "max by (pool) (orch_planner_desired_replicas)", legend="planner {{pool}}", x=0, y=8, w=12, h=8),
            _panel(4, "Pod phase", f"kube_pod_status_phase{{{pod}}}", legend="{{pod}} {{phase}}", x=12, y=8, w=12, h=8),
            _panel(5, "Replica healthy", "max by (pod) (orch_replica_healthy)", legend="{{pool}} {{pod}}", x=0, y=16, w=8, h=8),
            _panel(6, "Replica saturating", "max by (pod) (orch_replica_saturating)", legend="{{pool}} {{pod}}", x=8, y=16, w=8, h=8),
            _panel(7, "Restarts (last hour)",
                   f"increase(kube_pod_container_status_restarts_total{{{pod}}}[1h])",
                   legend="{{pod}}", x=16, y=16, w=8, h=8),
            _panel(8, "KV free by pod", "min by (pod) (orch_replica_kv_free_ratio)", legend="{{pool}} {{pod}}", x=0, y=24, w=12, h=8),
            _panel(9, "Tokens / waiting / running", "max by (pod) (orch_replica_tokens_in_flight)", legend="tokens {{pod}}", x=12, y=24, w=12, h=8),
            _panel(10, "Queue depth by pod", "max by (pod) (orch_replica_queue_depth)", legend="{{pod}}", x=0, y=32, w=12, h=8),
            _panel(11, "Waiting vs running", "max by (pod) (orch_replica_waiting)", legend="waiting {{pod}}",
                   x=12, y=32, w=12, h=8,
                   extra=[("max by (pod) (orch_replica_running)", "running {{pod}}")]),
        ],
        ["dodona", "replicas", "pods"],
    )

def vllm() -> dict:
    return _dash(
        "dodona-vllm",
        "Dodona / vLLM",
        [
            _panel(1, "GPU KV cache %", "vllm:kv_cache_usage_perc or vllm:gpu_cache_usage_perc", legend="{{instance}}", x=0, y=0, w=12, h=8),
            _panel(2, "Running / waiting", "vllm:num_requests_running", legend="running", x=12, y=0, w=12, h=8),
            _panel(3, "Waiting", "vllm:num_requests_waiting", legend="waiting", x=0, y=8, w=12, h=8),
            _panel(
                4,
                "TTFT p95",
                "histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))",
                x=12,
                y=8,
                w=12,
                h=8,
            ),
            _panel(
                5,
                "ITL p95",
                "histogram_quantile(0.95, sum by (le) (rate(vllm:inter_token_latency_seconds_bucket[5m])))",
                x=0,
                y=16,
                w=12,
                h=8,
            ),
            _panel(
                6,
                "e2e p95",
                "histogram_quantile(0.95, sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket[5m])))",
                x=12,
                y=16,
                w=12,
                h=8,
            ),
            _panel(7, "Prefix cache hits", "rate(vllm:prefix_cache_hits_total[5m])", x=0, y=24, w=8, h=8),
            _panel(8, "Prefix cache queries", "rate(vllm:prefix_cache_queries_total[5m])", x=8, y=24, w=8, h=8),
            _panel(9, "Preemptions", "rate(vllm:num_preemptions_total[5m])", x=16, y=24, w=8, h=8),
            _panel(10, "Prompt tokens / s", "rate(vllm:prompt_tokens_total[5m])", x=0, y=32, w=12, h=8),
            _panel(11, "Generation tokens / s", "rate(vllm:generation_tokens_total[5m])", x=12, y=32, w=12, h=8),
        ],
        ["dodona", "vllm"],
    )

def keda() -> dict:
    vllm = 'deployment=~"vllm-prefill|vllm-decode"'
    return _dash(
        "dodona-keda",
        "Dodona / KEDA",
        [
            _panel(1, "Scaler value", "keda_scaler_metrics_value or keda_scaler_metric_value", legend="{{scaledObject}} {{metric}} {{scaler}}", x=0, y=0, w=12, h=8),
            _panel(2, "Scaler active", "keda_scaler_active", legend="{{scaledObject}} {{scaler}}", x=12, y=0, w=12, h=8),
            _panel(3, "Spec replicas", f"kube_deployment_spec_replicas{{{vllm}}}", legend="{{deployment}} spec", x=0, y=8, w=12, h=8),
            _panel(4, "Status replicas", f"kube_deployment_status_replicas{{{vllm}}}", legend="{{deployment}} ready", x=12, y=8, w=12, h=8),
            _panel(5, "Trigger: tokens in flight", "sum by (phase) (orch_tokens_in_flight)", legend="{{phase}}", x=0, y=16, w=12, h=8),
            _panel(6, "Planner desired", "max by (pool) (orch_planner_desired_replicas)", legend="{{pool}}", x=12, y=16, w=12, h=8),
        ],
        ["dodona", "keda"],
    )

def hami() -> dict:
    return _dash(
        "dodona-hami",
        "Dodona / HAMi slices",
        [
            _panel(1, "Device memory allocated", "GPUDeviceMemoryAllocated or hami_gpu_memory_allocated_bytes or hami_vgpu_memory_allocated_bytes", legend="{{instance}} {{nodeid}}", x=0, y=0, w=12, h=8),
            _panel(2, "Device cores allocated", "GPUDeviceCoreAllocated or hami_gpu_core_allocated_ratio or hami_vgpu_core_allocated_ratio", legend="{{instance}}", x=12, y=0, w=12, h=8),
            _panel(3, "vGPU memory used", "vGPU_device_memory_usage_in_bytes or hami_vgpu_memory_used_bytes or hami_container_device_memory_bytes", legend="{{pod}} {{container_name}}", x=0, y=8, w=12, h=8),
            _panel(
                4,
                "Pod gpumem / gpucores limits",
                'kube_pod_container_resource_limits{resource=~"nvidia_com_gpumem|nvidia_com_gpucores"}',
                legend="{{pod}} {{resource}}",
                x=12,
                y=8,
                w=12,
                h=8,
            ),
            _panel(5, "Slice YAML reminder", "kube_pod_container_resource_limits", legend="compare nvidia.com/gpumem nvidia.com/gpucores vs nvidia-smi", x=0, y=16, w=24, h=6, kind="stat"),
        ],
        ["dodona", "hami"],
    )

#: What the KV connector actually did, as vLLM counts it. The old panels read
#: mooncake_hops_total / mooncake_blocks / mooncake_hop_tokens_total, which came
#: from router/mooncake.py — a Python dict that counts what our own router POSTs
#: to it. "Blocks in store" was a dict length, not KV blocks, and the series had
#: no exporter because that pod is never deployed. Replaced with the engine's
#: own counters, which are the only ones that move when bytes do.
_TRANSFERRED = 'vllm:prompt_tokens_by_source_total{source="external_kv_transfer"}'
KV_DELIVERED = f"sum({_TRANSFERRED})"
#: rate() must wrap the range vector, not the aggregation — sum(rate(x[5m])),
#: never rate(sum(x)[5m]). Grafana renders the latter as an empty panel.
KV_DELIVERED_RATE = f"sum(rate({_TRANSFERRED}[5m]))"
KV_ASKED = "sum(vllm:external_prefix_cache_queries_total)"
KV_SERVED = "sum(vllm:external_prefix_cache_hits_total)"


def mooncake() -> dict:
    return _dash(
        "dodona-mooncake",
        "Dodona / KV transfer (Mooncake connector)",
        [
            # The headline. Zero here means decode recomputed every prompt, no
            # matter what the router recorded.
            _panel(1, "KV tokens DELIVERED by the connector", KV_DELIVERED,
                   x=0, y=0, w=8, h=6, kind="stat"),
            _panel(2, "Connector hit rate",
                   f"{KV_SERVED} / clamp_min({KV_ASKED}, 1)",
                   x=8, y=0, w=8, h=6, kind="stat", unit="percentunit"),
            _panel(3, "Times the connector was asked", KV_ASKED,
                   x=16, y=0, w=8, h=6, kind="stat"),

            # The comparison that matters. Our router counts what it DECIDED to
            # hop; vLLM counts what arrived. Two lines, one panel, because shown
            # apart the first reads as throughput for a transfer that never
            # happened.
            _panel(4, "Claimed vs delivered — the router's record against the engine's count",
                   "sum(rate(orch_kv_transfer_tokens[5m]))",
                   legend="tokens/s the router CLAIMED to hop", x=0, y=6, w=24, h=9,
                   extra=[(KV_DELIVERED_RATE, "tokens/s the engine RECEIVED")]),

            _panel(5, "Hops recorded by the router (bookkeeping, not bytes)",
                   "sum(rate(orch_kv_transfer_total[5m]))", legend="records / s",
                   x=0, y=15, w=12, h=8),
            # Decode's OWN prefix cache. High here with zero delivered above is
            # the trap: it looks like the connector working and is not.
            _panel(6, "Decode's own prefix cache (NOT the connector)",
                   "sum(rate(vllm:prefix_cache_hits_total[5m])) / "
                   "clamp_min(sum(rate(vllm:prefix_cache_queries_total[5m])), 0.001)",
                   legend="local hit rate", x=12, y=15, w=12, h=8, unit="percentunit"),

            _panel(7, "KV evictions", "sum(rate(orch_kv_evict_total[5m]))",
                   legend="evicts / s", x=0, y=23, w=24, h=6),
        ],
        ["dodona", "mooncake"],
    )

def cluster() -> dict:
    pod = 'pod=~"vllm-.*|orch-serve.*|open-webui.*"'
    return _dash(
        "dodona-cluster",
        "Dodona / Cluster",
        [
            _panel(1, "Node Ready", 'kube_node_status_condition{condition="Ready",status="true"}', legend="{{node}}", x=0, y=0, w=8, h=6, kind="stat"),
            _panel(2, "CPU (node-exporter)", '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))', x=8, y=0, w=8, h=6, kind="stat", unit="percentunit"),
            _panel(3, "Mem available", "node_memory_MemAvailable_bytes", x=16, y=0, w=8, h=6, kind="stat", unit="decbytes"),
            _panel(4, "Pods by phase", "sum by (phase) (kube_pod_status_phase)", legend="{{phase}}", x=0, y=6, w=12, h=8),
            _panel(5, "Restarts (last hour)",
                   f"increase(kube_pod_container_status_restarts_total{{{pod}}}[1h])",
                   legend="{{pod}}", x=12, y=6, w=12, h=8),
            _panel(
                6,
                "Container CPU",
                f'sum by (pod) (rate(container_cpu_usage_seconds_total{{container!="",{pod}}}[5m]))',
                legend="{{pod}}",
                x=0,
                y=14,
                w=12,
                h=8,
            ),
            _panel(
                7,
                "Container memory",
                f'container_memory_working_set_bytes{{container!="",{pod}}}',
                legend="{{pod}}",
                x=12,
                y=14,
                w=12,
                h=8,
                unit="decbytes",
            ),
            _panel(8, "DCGM GPU util %", "DCGM_FI_DEV_GPU_UTIL", legend="H100 {{UUID}}", x=0, y=22, w=8, h=8, unit="percent"),
            _panel(9, "DCGM framebuffer used", "DCGM_FI_DEV_FB_USED * 1024 * 1024", legend="H100 FB used", x=8, y=22, w=8, h=8, unit="decbytes"),
            _panel(10, "DCGM power", "DCGM_FI_DEV_POWER_USAGE", legend="H100 power", x=16, y=22, w=8, h=8, unit="watt"),
        ],
        ["dodona", "cluster", "dcgm"],
    )

def errors() -> dict:
    return _dash(
        "dodona-errors",
        "Dodona / Success and failures",
        [
            _panel(1, "Requests / s", "sum(rate(orch_requests_total[5m]))", x=0, y=0, w=4, h=5, kind="stat"),
            _panel(2, "Success / s", "sum(rate(orch_completed_total[5m]))", x=4, y=0, w=4, h=5, kind="stat"),
            _panel(3, "Shed / s (admission fail)", "sum(rate(orch_shed_total[5m]))", x=8, y=0, w=4, h=5, kind="stat"),
            _panel(4, "Overflow / s", "sum(rate(orch_overflow_total[5m]))", x=12, y=0, w=4, h=5, kind="stat"),
            _panel(
                5,
                "Success ratio",
                "sum(rate(orch_completed_total[5m])) / clamp_min(sum(rate(orch_requests_total[5m])), 1e-9)",
                x=16,
                y=0,
                w=4,
                h=5,
                kind="stat",
            ),
            _panel(
                6,
                "Shed ratio",
                "sum(rate(orch_shed_total[5m])) / clamp_min(sum(rate(orch_requests_total[5m])), 1e-9)",
                x=20,
                y=0,
                w=4,
                h=5,
                kind="stat",
            ),
            _panel(
                7,
                "Success vs shed vs overflow",
                "sum(rate(orch_completed_total[5m]))",
                legend="success (200)",
                extra=[
                    ("sum(rate(orch_shed_total[5m]))", "shed (429/503)"),
                    ("sum(rate(orch_overflow_total[5m]))", "overflow"),
                    ("sum(rate(orch_requests_total[5m]))", "requests"),
                ],
                x=0,
                y=5,
                w=16,
                h=9,
            ),
            _panel(
                8,
                "Sheds by reason / HTTP code",
                "sum by (reason, code) (rate(orch_shed_total[5m]))",
                legend="{{reason}} {{code}}",
                x=16,
                y=5,
                w=8,
                h=9,
            ),
            _panel(
                9,
                "Placed by capability",
                "sum by (capability) (rate(orch_place_total[5m]))",
                legend="{{capability}}",
                x=0,
                y=14,
                w=12,
                h=8,
            ),
            _panel(
                10,
                "Gateway p95",
                'histogram_quantile(0.95, sum by (le) (rate(orch_request_duration_seconds_bucket{stage="gateway"}[5m])))',
                legend="p95",
                x=12,
                y=14,
                w=12,
                h=8,
            ),
        ],
        ["dodona", "errors", "admission"],
    )

DASHBOARDS = {
    "overview": overview,
    "gateway": gateway,
    "router": router,
    "replicas": replicas,
    "vllm": vllm,
    "keda": keda,
    "hami": hami,
    "mooncake": mooncake,
    "cluster": cluster,
    "errors": errors,
}

def write_dashboards(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, builder in DASHBOARDS.items():
        path = dest / f"{name}.json"
        path.write_text(json.dumps(builder(), indent=2) + "\n")
        written.append(path)
    return written

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    write_dashboards(root / "k8s" / "observability" / "dashboards")
