#!/usr/bin/env python3
"""Four dashboards that read as one story, for the process-path fleet.

The Kubernetes path has eleven dashboards because it has eleven things to talk
about — pods, slices, autoscalers. Here there are three vLLM processes, a
gateway and three GPUs, and the only question worth a screen is:

    a request arrives
      -> the gateway admits it, queues it, routes it        (1 gateway)
      -> an engine prefills it, then decodes it             (2 engines)
      -> the router books a KV hop prefill -> decode         (3 kv)
      -> all of which is a card getting hot                 (4 hardware)

So: four dashboards, numbered, each one stage of that path, every panel built
only from metrics this fleet actually exports. `EXPORTED` below is that list,
taken from the live Prometheus on a running fleet, and
``test_runpod_dashboards.py`` fails the build if a panel queries anything else.
That test is the whole point of this file — the previous set put eleven
Kubernetes dashboards in front of an audience and four were permanently blank.

Unlike the k8s path, `mooncake_*` IS exported here: `workers.sh` starts the hop
ledger as a process and the smoke test asserts it answers. The KV dashboard
uses it — and says plainly that a ledger is ALL it is on this path, because
`--kv-transfer-config` is not passed and so decode re-prefills. See `kv()` and
`docs/project/topology.md`. A dashboard that implied the bytes moved would
mislead an audience about the project's own central claim.

    python3 infra/runpod-cluster/observability/dashboards.py [DEST]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}

#: Every metric family the process-path fleet exports, by the component that
#: exports it. Read off `/api/v1/label/__name__/values` on a running fleet —
#: not from vLLM's docs, which list metrics this version does not emit, and not
#: from the k8s dashboards, which query a Kubernetes that is not here.
EXPORTED = {
    "gateway": (
        "orch_requests_total",
        "orch_completed_total",
        "orch_shed_total",
        "orch_overflow_total",
        "orch_place_total",
        "orch_pick_total",
        "orch_pick_unknown_snapshot_total",
        "orch_sticky_total",
        "orch_request_duration_seconds",
        "orch_kv_free_ratio",
        "orch_kv_transfer_total",
        "orch_kv_transfer_tokens",
        "orch_kv_evict_total",
        "orch_tokens_in_flight",
        "orch_replica_healthy",
        "orch_replica_saturating",
        "orch_replica_kv_free_ratio",
        "orch_replica_queue_depth",
        "orch_replica_active_requests",
        "orch_replica_running",
        "orch_replica_waiting",
        "orch_replica_tokens_in_flight",
        "orch_planner_desired_replicas",
        "admitperf_gate_active",
    ),
    "vllm": (
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
        "vllm:kv_cache_usage_perc",
        "vllm:time_to_first_token_seconds",
        "vllm:inter_token_latency_seconds",
        "vllm:e2e_request_latency_seconds",
        "vllm:request_queue_time_seconds",
        "vllm:request_prefill_time_seconds",
        "vllm:request_decode_time_seconds",
        "vllm:request_inference_time_seconds",
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
        "vllm:prompt_tokens_cached_total",
        "vllm:prefix_cache_hits_total",
        "vllm:prefix_cache_queries_total",
        "vllm:external_prefix_cache_hits_total",
        "vllm:external_prefix_cache_queries_total",
        "vllm:request_prefill_kv_computed_tokens",
        "vllm:num_preemptions_total",
        "vllm:iteration_tokens_total",
        "vllm:request_success_total",
    ),
    "mooncake": (
        "mooncake_hops_total",
        "mooncake_hop_tokens_total",
        "mooncake_blocks",
    ),
    "dcgm": (
        "DCGM_FI_DEV_GPU_UTIL",
        "DCGM_FI_DEV_MEM_COPY_UTIL",
        "DCGM_FI_DEV_FB_USED",
        "DCGM_FI_DEV_FB_FREE",
        "DCGM_FI_DEV_FB_TOTAL",
        "DCGM_FI_DEV_POWER_USAGE",
        "DCGM_FI_DEV_ENFORCED_POWER_LIMIT",
        "DCGM_FI_DEV_GPU_TEMP",
        "DCGM_FI_DEV_SM_CLOCK",
        "DCGM_FI_DEV_MEMORY_CLOCK",
    ),
}

#: The tag that makes the four link to each other. Grafana renders a dashboard
#: link as a row of buttons at the top, so the path is walkable in the order it
#: happens instead of through a search box.
STORY_TAG = "dodona-path"


def _target(expr: str, legend: str = "", ref: str = "A") -> dict:
    return {"datasource": PROM, "editorMode": "code", "expr": expr,
            "legendFormat": legend or "{{__name__}}", "range": True, "refId": ref}


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
    desc: str = "",
    thresholds: list[tuple[str, float | None]] | None = None,
    maximum: float | None = None,
) -> dict:
    targets = [_target(expr, legend, "A")]
    for i, (ex, leg) in enumerate(extra or []):
        targets.append(_target(ex, leg, chr(ord("B") + i)))
    defaults: dict = {"custom": {"drawStyle": "line", "fillOpacity": 10, "lineWidth": 1}}
    if unit:
        defaults["unit"] = unit
    if maximum is not None:
        defaults["max"] = maximum
        defaults["min"] = 0
    if thresholds:
        # `area` so breaching 0.90 KV colours the panel rather than drawing a
        # line nobody notices while talking.
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": c, "value": v} for c, v in thresholds],
        }
        defaults["custom"]["thresholdsStyle"] = {"mode": "area"}
    panel = {
        "id": pid,
        "type": kind,
        "title": title,
        "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": PROM,
        "targets": targets,
        "options": {"legend": {"displayMode": "list", "placement": "bottom",
                               "showLegend": True}},
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


def _text(pid: int, content: str, *, x: int = 0, y: int = 0, w: int = 24, h: int = 4) -> dict:
    """A markdown panel saying where in the request path this screen sits.

    Worth the vertical space: the panels answer "what is the number", and this
    answers "why am I looking at this one", which is the thing an audience
    cannot recover from a legend.
    """
    return {
        "id": pid,
        "type": "text",
        "title": "",
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {"mode": "markdown", "content": content},
        "transparent": True,
    }


def _dash(uid: str, title: str, panels: list[dict], tags: list[str]) -> dict:
    return {
        "uid": uid,
        "title": title,
        "tags": [*tags, STORY_TAG],
        "schemaVersion": 39,
        "version": 2,
        "timezone": "browser",
        "refresh": "5s",
        "time": {"from": "now-30m", "to": "now"},
        "templating": {"list": []},
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        # keepTime, so clicking through the path keeps the window you were
        # looking at. Without it every hop resets to now-30m and the spike you
        # were following disappears.
        "links": [{
            "type": "dashboards", "title": "the request path", "tags": [STORY_TAG],
            "asDropdown": False, "icon": "external link", "includeVars": False,
            "keepTime": True, "targetBlank": False, "tooltip": "", "url": "",
        }],
        "liveNow": True,
        "panels": panels,
        "style": "dark",
    }


def gateway() -> dict:
    """Stage 1: admit, queue, route. Everything here is the gateway's own view.

    Every query aggregates. The gateway process restarts on every
    `make fleet-runpod`, and an unaggregated orch_* query draws one line per
    restart for ever.
    """
    story = (
        "## 1 · Gateway — admit, queue, route\n"
        "A request arrives here first. The gateway **guards** it (known model? "
        "sane `max_tokens`?), **admits or sheds** it against KV pressure, "
        "**places** it on a pool, and **picks** a replica. Nothing below has "
        "touched a GPU yet — this is the decision, and it costs about 90 ms.\n\n"
        "Next: **2 · Engines**, where the request becomes tokens."
    )
    return _dash("dodona-gateway", "Dodona / 1 · Gateway — admit, queue, route", [
        _text(1, story),

        _panel(2, "Arriving", "sum(rate(orch_requests_total[1m]))",
               legend="req/s", kind="stat", x=0, y=4, w=6, h=4, unit="reqps",
               desc="Requests the gateway accepted for processing, per second."),
        _panel(3, "Shed", "sum(rate(orch_shed_total[1m]))",
               legend="503/s", kind="stat", x=6, y=4, w=6, h=4, unit="reqps",
               desc="Turned away with 503. Non-zero means admission control is "
                    "doing something — which is the point of the exercise."),
        _panel(4, "Admission policy", "max by (policy) (admitperf_gate_active)",
               legend="{{policy}}", kind="stat", x=12, y=4, w=6, h=4,
               desc="1 while the policy named in the legend is deciding. "
                    "policy=none means requests are admitted unconditionally."),
        _panel(5, "Gateway p95",
               "histogram_quantile(0.95, sum by (le) "
               "(rate(orch_request_duration_seconds_bucket[5m])))",
               legend="p95", kind="stat", x=18, y=4, w=6, h=4, unit="s",
               desc="Time inside the gateway, end to end, including the engine "
                    "it routed to."),

        _panel(6, "Arrivals vs completions vs shed",
               "sum(rate(orch_requests_total[1m]))", legend="arrived",
               x=0, y=8, unit="reqps",
               extra=[("sum(rate(orch_completed_total[1m]))", "completed"),
                      ("sum(rate(orch_shed_total[1m]))", "shed (503)")],
               desc="The three lines should sum: what arrives either completes "
                    "or is shed. A gap between arrived and completed+shed is "
                    "requests still in flight."),
        _panel(7, "Gateway latency p50 / p95 / p99",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(orch_request_duration_seconds_bucket[5m])))",
               legend="p50", x=12, y=8, unit="s",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(orch_request_duration_seconds_bucket[5m])))", "p95"),
                      ("histogram_quantile(0.99, sum by (le) "
                       "(rate(orch_request_duration_seconds_bucket[5m])))", "p99")]),

        _panel(8, "Why requests were shed",
               "sum by (reason) (rate(orch_shed_total[5m]))", legend="{{reason}}",
               x=0, y=16, unit="reqps",
               desc="reason=no_eligible_pod means every replica was over its KV "
                    "threshold. That is admission control working, not a fault."),
        _panel(9, "Overflow to the other pool",
               "sum(rate(orch_overflow_total[5m]))", legend="overflowed",
               x=12, y=16, unit="reqps",
               desc="A decode request sent to the prefill pool (or the reverse) "
                    "because its own pool had no headroom."),

        _panel(10, "Queue depth per replica",
               "max by (pod, pool) (orch_replica_queue_depth)",
               legend="{{pod}} ({{pool}})", x=0, y=24,
               desc="Queued at the engine, as the gateway sees it. This is the "
                    "backlog place() is trying to even out."),
        _panel(11, "Where requests were placed",
               "sum by (capability) (rate(orch_place_total[5m]))",
               legend="{{capability}}", x=12, y=24, unit="reqps",
               desc="place() picks a pool by capability. With two decode "
                    "workers answering, this is a real choice."),

        _panel(12, "Picks vs sticky reuse",
               "sum(rate(orch_pick_total[5m]))", legend="picks",
               x=0, y=32, unit="reqps",
               extra=[("sum(rate(orch_sticky_total[5m]))", "sticky (prefix reuse)")],
               desc="A sticky pick sends a request back to the replica that "
                    "already holds its prefix, so the KV does not have to be "
                    "recomputed. Compare with prefix cache hits on 3 · KV."),
        _panel(13, "Picks on a stale snapshot",
               "sum(rate(orch_pick_unknown_snapshot_total[5m]))", legend="stale",
               x=12, y=32, unit="reqps",
               desc="The router chose using replica state it could not confirm. "
                    "Non-zero under load means decisions are outrunning scrapes."),

        _panel(14, "KV headroom per replica — the signal place() reads",
               "min by (pod, pool) (orch_replica_kv_free_ratio)",
               legend="{{pod}} ({{pool}})", x=0, y=40, unit="percentunit",
               maximum=1, thresholds=[("green", None), ("red", 0.10)],
               desc="Free fraction of each replica's KV cache. The gateway sheds "
                    "when this bottoms out; red is under 10% free."),
        _panel(15, "Replica health: healthy vs saturating",
               "min by (pod) (orch_replica_healthy)", legend="healthy {{pod}}",
               x=12, y=40, maximum=1,
               extra=[("max by (pod) (orch_replica_saturating)", "saturating {{pod}}")],
               desc="Saturating is the earlier signal: the replica still answers "
                    "but is close enough to its limit that the router avoids it."),

        _panel(16, "Tokens in flight by phase",
               "sum by (phase) (orch_tokens_in_flight)", legend="{{phase}}",
               x=0, y=48,
               desc="An instantaneous gauge against a 15s scrape, so it reads low "
                    "unless the fleet is genuinely busy. Use the token rates on "
                    "2 · Engines for throughput."),
        _panel(17, "Active requests per replica",
               "max by (pod, pool) (orch_replica_active_requests)",
               legend="{{pod}} ({{pool}})", x=12, y=48,
               desc="What the gateway believes each replica is working on. "
                    "Compare with 'running' on 2 · Engines: a persistent "
                    "disagreement means stale snapshots."),
    ], ["gateway", "admission", "routing"])


def engine() -> dict:
    """Stage 2: the three vLLM processes, split prefill and decode.

    Every panel keeps the `pod` and `phase` labels Prometheus attaches to the
    vLLM targets, because the interesting comparison is always prefill against
    decode: they do different work and fail differently.
    """
    story = (
        "## 2 · Engines — prefill and decode\n"
        "Three vLLM processes, one per card. **prefill** (`:8100`) turns the "
        "prompt into KV; **decode-0** and **decode-1** (`:8101`, `:8102`) "
        "generate tokens from it. They are the same model and the same binary — "
        "what differs is the shape of the work, which is why their latency and "
        "KV curves look nothing like each other.\n\n"
        "Prefill is compute-bound and bursty. Decode is memory-bound and "
        "steady, and it is decode that fills the KV cache.\n\n"
        "Next: **3 · KV**, how prefill's work reaches decode."
    )
    return _dash("dodona-engine", "Dodona / 2 · Engines — prefill and decode", [
        _text(1, story),

        _panel(2, "Running", "sum(vllm:num_requests_running)", legend="running",
               kind="stat", x=0, y=4, w=6, h=4,
               desc="Requests on a GPU right now, all three engines."),
        _panel(3, "Waiting", "sum(vllm:num_requests_waiting)", legend="waiting",
               kind="stat", x=6, y=4, w=6, h=4,
               desc="Queued inside the engines. Rises before latency does."),
        _panel(4, "KV used — decode (max)",
               "max(vllm:kv_cache_usage_perc{phase=\"decode\"})", legend="decode",
               kind="stat", x=12, y=4, w=6, h=4, unit="percentunit",
               desc="The fullest decode worker's KV cache. The admission policy "
                    "is thresholded at 0.90; if this never approaches it, the "
                    "run is INERT and proves nothing."),
        _panel(5, "KV used — prefill",
               "max(vllm:kv_cache_usage_perc{phase=\"prefill\"})", legend="prefill",
               kind="stat", x=18, y=4, w=6, h=4, unit="percentunit",
               desc="Prefill holds KV only until it hops to decode, so this "
                    "stays low even while prefill is busy."),

        _panel(6, "Requests running, per engine",
               "sum by (pod, phase) (vllm:num_requests_running)",
               legend="{{pod}} ({{phase}})", x=0, y=8,
               desc="Two decode lines moving independently is place() spreading "
                    "load; one flat at zero means it is not."),
        _panel(7, "Requests waiting, per engine",
               "sum by (pod, phase) (vllm:num_requests_waiting)",
               legend="{{pod}} ({{phase}})", x=12, y=8,
               desc="An engine-side queue, distinct from the gateway's view on "
                    "screen 1."),

        _panel(8, "KV cache usage, per engine",
               "max by (pod, phase) (vllm:kv_cache_usage_perc)",
               legend="{{pod}} ({{phase}})", x=0, y=16, unit="percentunit",
               maximum=1, thresholds=[("green", None), ("orange", 0.75), ("red", 0.90)],
               desc="The signal the whole project is about. Red is 0.90, where "
                    "the admission policy starts shedding."),
        _panel(9, "Preemptions",
               "sum by (pod, phase) (rate(vllm:num_preemptions_total[5m]))",
               legend="{{pod}} ({{phase}})", x=12, y=16,
               desc="vLLM evicting a running request's KV to make room. Every "
                    "preemption is work thrown away and later redone — this is "
                    "what admission control exists to prevent."),

        _panel(10, "Time to first token — p50 / p95 / p99",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(vllm:time_to_first_token_seconds_bucket[5m])))",
               legend="p50", x=0, y=24, unit="s",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:time_to_first_token_seconds_bucket[5m])))", "p95"),
                      ("histogram_quantile(0.99, sum by (le) "
                       "(rate(vllm:time_to_first_token_seconds_bucket[5m])))", "p99")],
               desc="What the listener experiences as lag before speech starts. "
                    "Dominated by prefill plus queueing."),
        _panel(11, "Inter-token latency — p50 / p95",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(vllm:inter_token_latency_seconds_bucket[5m])))",
               legend="p50", x=12, y=24, unit="s",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:inter_token_latency_seconds_bucket[5m])))", "p95")],
               desc="Gap between tokens once generation starts. This is what "
                    "makes synthesised speech stutter."),

        _panel(12, "Prefill time p95, per engine",
               "histogram_quantile(0.95, sum by (le, pod) "
               "(rate(vllm:request_prefill_time_seconds_bucket[5m])))",
               legend="{{pod}}", x=0, y=32, unit="s",
               desc="Turning the prompt into KV. Scales with prompt length."),
        _panel(13, "Decode time p95, per engine",
               "histogram_quantile(0.95, sum by (le, pod) "
               "(rate(vllm:request_decode_time_seconds_bucket[5m])))",
               legend="{{pod}}", x=12, y=32, unit="s",
               desc="Generating the response. Scales with tokens produced."),

        _panel(14, "Queue time p95, per engine",
               "histogram_quantile(0.95, sum by (le, pod) "
               "(rate(vllm:request_queue_time_seconds_bucket[5m])))",
               legend="{{pod}}", x=0, y=40, unit="s",
               desc="Waiting inside the engine before work starts. Queue time "
                    "climbing while prefill and decode time stay flat means the "
                    "fleet is admission-limited, not compute-limited."),
        _panel(15, "End-to-end request latency p50 / p95 / p99",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(vllm:e2e_request_latency_seconds_bucket[5m])))",
               legend="p50", x=12, y=40, unit="s",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:e2e_request_latency_seconds_bucket[5m])))", "p95"),
                      ("histogram_quantile(0.99, sum by (le) "
                       "(rate(vllm:e2e_request_latency_seconds_bucket[5m])))", "p99")]),

        _panel(16, "Prompt tokens/s vs generated tokens/s",
               "sum by (phase) (rate(vllm:prompt_tokens_total[1m]))",
               legend="prompt ({{phase}})", x=0, y=48,
               extra=[("sum by (phase) (rate(vllm:generation_tokens_total[1m]))",
                       "generated ({{phase}})")],
               desc="Prompt tokens land on prefill, generated tokens on decode. "
                    "The ratio is the shape of your workload."),
        _panel(17, "Batch size (tokens per engine iteration) p50 / p95",
               "histogram_quantile(0.50, sum by (le, pod) "
               "(rate(vllm:iteration_tokens_total_bucket[5m])))",
               legend="p50 {{pod}}", x=12, y=48,
               extra=[("histogram_quantile(0.95, sum by (le, pod) "
                       "(rate(vllm:iteration_tokens_total_bucket[5m])))", "p95 {{pod}}")],
               desc="How much work vLLM batched per step. Larger batches mean "
                    "better GPU efficiency and worse tail latency."),

        _panel(18, "Successful requests/s, per engine",
               "sum by (pod, phase) (rate(vllm:request_success_total[5m]))",
               legend="{{pod}} ({{phase}})", x=0, y=56, unit="reqps",
               desc="Completed by the engine. Compare with the gateway's "
                    "completion rate on screen 1."),
        _panel(19, "Local prefix cache hit rate",
               "sum(rate(vllm:prefix_cache_hits_total[5m])) / "
               "clamp_min(sum(rate(vllm:prefix_cache_queries_total[5m])), 1)",
               legend="hit rate", x=12, y=56, unit="percentunit", maximum=1,
               desc="Prompt prefixes already in this engine's cache, so prefill "
                    "was skipped. clamp_min keeps the panel at 0 instead of "
                    "blank when there is no traffic."),
    ], ["vllm", "engine"])


def kv() -> dict:
    """Stage 3: the KV cache, and the hop — which on this path is a LEDGER.

    Read `docs/project/topology.md` before presenting this screen. A real split
    needs three things: placement, orchestration, and transfer. This fleet has
    the first two. It does not have the third, because `--kv-transfer-config` is
    Kubernetes-specific as written (RDMA `mlx5_0`, `vllm-prefill:8998`) and
    `infra/setup/engine.sh` does not pass it.

    So no KV bytes move here. `KVBus.transfer` writes a row — `{src, dst,
    req_id, tokens}` — and POSTs it to the mooncake ledger; `router/nccl.py` and
    `router/nixl.py` are `return`. Transfer lives INSIDE the engine, because
    vLLM owns the block table, and no amount of control-plane bookkeeping can
    hand blocks to another process.

    Every panel below is therefore an honest count of a DECISION, not of a byte.
    The wording says so, because a dashboard that implies otherwise is the one
    place this project could mislead someone about its own central claim.
    """
    story = (
        "## 3 · KV — the hop, as a routing decision\n"
        "Prefill and decode run on **different cards**, so the KV prefill "
        "computes would have to reach decode. The mooncake ledger on `:50051` "
        "records every hop the router decided to make.\n\n"
        "**On this fleet the ledger is all there is.** Moving the blocks needs "
        "`--kv-transfer-config` inside vLLM, and the process path does not pass "
        "it — so decode **re-prefills the whole prompt**, and the prompt is "
        "prefilled twice to produce one answer. Placement and orchestration are "
        "real; the transfer is not.\n\n"
        "That makes this the most useful screen here: **external prefix cache "
        "hits sitting at zero is the proof**, and it is what a real connector "
        "would change. See `docs/project/topology.md`.\n\n"
        "Next: **4 · Hardware**, the cards all of this runs on."
    )
    return _dash("dodona-kv", "Dodona / 3 · KV — the hop (a routing decision here)", [
        _text(1, story),

        _panel(2, "Hops decided", "sum(mooncake_hops_total)", legend="hops",
               kind="stat", x=0, y=4, w=6, h=4,
               desc="Times the router routed a request prefill -> decode since "
                    "the ledger started. A decision count, not a transfer "
                    "count: nothing moved."),
        _panel(3, "Tokens moved", "sum(mooncake_hop_tokens_total)", legend="tokens",
               kind="stat", x=6, y=4, w=6, h=4,
               desc="Tokens of KV that a real connector WOULD have moved. "
                    "Here decode recomputed all of them, so read this as the "
                    "size of the transfer the architecture is asking for."),
        _panel(4, "Blocks in the ledger", "sum(mooncake_blocks)", legend="blocks",
               kind="stat", x=12, y=4, w=6, h=4,
               desc="KV blocks the ledger is currently tracking."),
        _panel(5, "Fleet KV headroom", "min(orch_kv_free_ratio)", legend="free",
               kind="stat", x=18, y=4, w=6, h=4, unit="percentunit",
               desc="Free fraction across the fleet, as the gateway computes it."),

        _panel(6, "Hop rate",
               "sum(rate(mooncake_hops_total[1m]))", legend="hops/s",
               x=0, y=8,
               desc="One per request that prefilled on one card and decoded on "
                    "another, so under normal routing this tracks arrivals. It "
                    "measures the router, not the network."),
        _panel(7, "Tokens hopped per second",
               "sum(rate(mooncake_hop_tokens_total[1m]))", legend="tokens/s",
               x=12, y=8,
               desc="What the hop WOULD cost in bandwidth. At 256 KiB of KV per "
                    "token for this model, 1000 tokens/s is ~250 MiB/s — the "
                    "number to weigh against recomputing. No bytes move here."),

        _panel(8, "Transfers the gateway recorded vs tokens transferred",
               "sum(rate(orch_kv_transfer_total[5m]))", legend="transfers/s",
               x=0, y=16,
               extra=[("sum(rate(orch_kv_transfer_tokens[5m]))", "tokens/s")],
               desc="The gateway's own count, independent of the ledger. The two "
                    "should agree; a divergence means one of them is missing "
                    "events."),
        _panel(9, "KV evictions",
               "sum(rate(orch_kv_evict_total[5m]))", legend="evictions/s",
               x=12, y=16,
               desc="KV dropped to make room. Every eviction is a future "
                    "recompute — the cost admission control is trading against. "
                    "This one is real: the pool it evicts from is vLLM's own."),

        _panel(10, "KV usage: prefill vs decode",
               "max by (phase) (vllm:kv_cache_usage_perc)", legend="{{phase}}",
               x=0, y=24, unit="percentunit", maximum=1,
               thresholds=[("green", None), ("orange", 0.75), ("red", 0.90)],
               desc="The asymmetry that makes disaggregation work: prefill's KV "
                    "drains after each hop, decode's accumulates for the life of "
                    "the request."),
        _panel(11, "KV headroom per pool",
               "min by (pool) (orch_replica_kv_free_ratio)", legend="{{pool}}",
               x=12, y=24, unit="percentunit", maximum=1,
               desc="Free fraction by pool. Decode is the one that runs out."),

        _panel(12, "Prefill skipped: cached vs computed prompt tokens",
               "sum(rate(vllm:prompt_tokens_cached_total[5m]))", legend="cached (reused)",
               x=0, y=32,
               extra=[("sum(rate(vllm:prompt_tokens_total[5m]))", "total prompt tokens")],
               desc="Cached tokens had their KV already and were not prefilled "
                    "again. The gap between the lines is real prefill work."),
        _panel(13, "External prefix cache hit rate",
               "sum(rate(vllm:external_prefix_cache_hits_total[5m])) / "
               "clamp_min(sum(rate(vllm:external_prefix_cache_queries_total[5m])), 1)",
               legend="hit rate", x=12, y=32, unit="percentunit", maximum=1,
               desc="KV found outside this engine — i.e. arriving across a hop. "
                    "Flat at zero is the honest signal that no connector is "
                    "installed and decode re-prefilled. Lifting this off zero "
                    "is exactly what --kv-transfer-config would do."),

        _panel(14, "KV tokens computed during prefill p50 / p95",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(vllm:request_prefill_kv_computed_tokens_bucket[5m])))",
               legend="p50", x=0, y=40,
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:request_prefill_kv_computed_tokens_bucket[5m])))",
                       "p95")],
               desc="Per request, how many tokens of KV prefill actually had to "
                    "build. Lower than prompt length means cache reuse."),
        _panel(15, "Sticky routing — requests sent back to the replica holding their KV",
               "sum(rate(orch_sticky_total[5m]))", legend="sticky/s",
               x=12, y=40, unit="reqps",
               desc="The cheapest possible outcome: no hop and no recompute, "
                    "because the KV is already where the request landed."),
    ], ["kv", "mooncake", "disaggregation"])


def hardware() -> dict:
    """Stage 4: the three A100s, each labelled with the worker pinned to it.

    `pod` and `phase` are not DCGM labels — the real exporter has no idea what
    is running on a card. gpu_exporter.py adds them from the mapping workers.sh
    uses to pin workers, which is what makes "is the *prefill* card saturated?"
    a question this screen can answer.
    """
    story = (
        "## 4 · Hardware — the three A100s\n"
        "Where everything above actually happens. Each card is labelled with "
        "the worker pinned to it, so `GPU 0 · prefill` is the card doing prompt "
        "work and `GPU 1 · decode-0` / `GPU 2 · decode-1` are generating.\n\n"
        "Read this against **2 · Engines**: utilisation high with KV low is "
        "compute-bound prefill; KV high with utilisation low is memory-bound "
        "decode, and no amount of GPU is going to help — which is the argument "
        "for admission control rather than more cards.\n\n"
        "Back to **1 · Gateway** to follow another request."
    )
    return _dash("dodona-hardware", "Dodona / 4 · Hardware — GPUs", [
        _text(1, story),

        _panel(2, "GPUs reporting", "count(DCGM_FI_DEV_GPU_UTIL)", legend="cards",
               kind="stat", x=0, y=4, w=6, h=4,
               desc="Should be 3. Fewer means a card fell out of nvidia-smi."),
        _panel(3, "Fleet power draw", "sum(DCGM_FI_DEV_POWER_USAGE)", legend="watts",
               kind="stat", x=6, y=4, w=6, h=4, unit="watt",
               desc="All cards together. Idle A100s draw ~60 W each; under load "
                    "they approach their 400 W limit."),
        _panel(4, "Hottest card", "max(DCGM_FI_DEV_GPU_TEMP)", legend="degC",
               kind="stat", x=12, y=4, w=6, h=4, unit="celsius",
               desc="A100s throttle in the mid-80s."),
        _panel(5, "Framebuffer in use", "sum(DCGM_FI_DEV_FB_USED) / 1024",
               legend="GiB", kind="stat", x=18, y=4, w=6, h=4, unit="gbytes",
               desc="Weights plus KV cache across all three cards. nvidia-smi "
                    "reports MiB, so this divides by 1024."),

        _panel(6, "GPU utilisation, per card",
               "max by (gpu, pod) (DCGM_FI_DEV_GPU_UTIL)",
               legend="GPU {{gpu}} · {{pod}}", x=0, y=8, unit="percent",
               maximum=100,
               desc="Share of time the SMs were busy. Prefill spikes; decode "
                    "sits lower because it is waiting on memory, not maths."),
        _panel(7, "Memory bandwidth utilisation, per card",
               "max by (gpu, pod) (DCGM_FI_DEV_MEM_COPY_UTIL)",
               legend="GPU {{gpu}} · {{pod}}", x=12, y=8, unit="percent",
               maximum=100,
               desc="Decode is memory-bound, so this is the number that caps it. "
                    "High here with low GPU utilisation is the signature."),

        _panel(8, "Framebuffer used, per card",
               "max by (gpu, pod) (DCGM_FI_DEV_FB_USED)",
               legend="GPU {{gpu}} · {{pod}}", x=0, y=16, unit="decmbytes",
               desc="Model weights (~19 GiB for the 32B AWQ) plus whatever KV "
                    "vLLM reserved. Flat by design: vLLM preallocates."),
        _panel(9, "Framebuffer used, as a share of the card",
               "max by (gpu, pod) (DCGM_FI_DEV_FB_USED) / "
               "clamp_min(max by (gpu, pod) (DCGM_FI_DEV_FB_TOTAL), 1)",
               legend="GPU {{gpu}} · {{pod}}", x=12, y=16, unit="percentunit",
               maximum=1,
               desc="What --gpu-memory-utilization actually produced. Should sit "
                    "near the value workers.sh passed; far below it means the KV "
                    "pool is too small for the run to reach its threshold."),

        _panel(10, "Power draw vs enforced limit",
               "max by (gpu, pod) (DCGM_FI_DEV_POWER_USAGE)",
               legend="draw · GPU {{gpu}} {{pod}}", x=0, y=24, unit="watt",
               extra=[("max by (gpu) (DCGM_FI_DEV_ENFORCED_POWER_LIMIT)",
                       "limit · GPU {{gpu}}")],
               desc="Draw pressed against the limit means the card is power "
                    "capped and clocks will drop."),
        _panel(11, "Temperature, per card",
               "max by (gpu, pod) (DCGM_FI_DEV_GPU_TEMP)",
               legend="GPU {{gpu}} · {{pod}}", x=12, y=24, unit="celsius",
               thresholds=[("green", None), ("orange", 75), ("red", 85)],
               desc="Red is the throttling range. In a rented pod you share "
                    "airflow with other tenants, so this can move without you."),

        _panel(12, "SM clock, per card",
               "max by (gpu, pod) (DCGM_FI_DEV_SM_CLOCK)",
               legend="GPU {{gpu}} · {{pod}}", x=0, y=32, unit="rothz",
               desc="Drops when the card is thermally or power throttled. A clock "
                    "falling while utilisation stays high explains throughput "
                    "that degrades without any change in load."),
        _panel(13, "Memory clock, per card",
               "max by (gpu, pod) (DCGM_FI_DEV_MEMORY_CLOCK)",
               legend="GPU {{gpu}} · {{pod}}", x=12, y=32, unit="rothz",
               desc="HBM clock. Constant on a healthy A100."),

        _panel(14, "Utilisation by role: prefill vs decode",
               "avg by (phase) (DCGM_FI_DEV_GPU_UTIL)", legend="{{phase}}",
               x=0, y=40, unit="percent", maximum=100,
               extra=[("avg by (phase) (DCGM_FI_DEV_MEM_COPY_UTIL)",
                       "{{phase}} memory bw")],
               desc="The whole argument on one panel: the two roles load the "
                    "hardware differently, which is why they get separate pools "
                    "and separate scaling decisions."),
        _panel(15, "Framebuffer free, per card",
               "min by (gpu, pod) (DCGM_FI_DEV_FB_FREE)",
               legend="GPU {{gpu}} · {{pod}}", x=12, y=40, unit="decmbytes",
               desc="Headroom left on the card. Near zero is where vLLM would "
                    "fail to allocate — distinct from the KV cache being full, "
                    "which is a pool vLLM already owns."),
    ], ["hardware", "dcgm", "gpu"])


#: In the order a request travels. The numbers are in the titles so Grafana's
#: alphabetical list is also the path.
DASHBOARDS = {
    "gateway": gateway,
    "engine": engine,
    "kv": kv,
    "hardware": hardware,
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
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "dashboards")
    for p in write_dashboards(out):
        print(p)
