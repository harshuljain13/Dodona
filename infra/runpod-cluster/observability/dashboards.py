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

#: How a per-engine series is labelled.
#:
#: Just the pod. `phase` on a vLLM target is "both" on this fleet, because
#: every engine serves both phases — so printing it put "(both)" on every line
#: of every legend. Noise repeated down a legend teaches people not to read
#: legends.
POD = "{{pod}}"


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


def overview() -> dict:
    """Screen 0: the one you leave up.

    Everything else on this path answers "why". This answers "what is happening
    right now", in the numbers a listener actually experiences — time to first
    token, the gap between tokens, how many requests are in flight, and whether
    anything is being refused.

    Deliberately no per-replica breakdowns: that is screen 1 and 2's job. A
    dashboard that tries to be all of them is one nobody reads under pressure.
    """
    story = (
        "## 0 · Overview — is it healthy, and is it fast?\n"
        "The numbers a **listener** feels, not the ones a cluster reports. "
        "**TTFT** is silence before the answer starts. **Inter-token latency** "
        "is whether speech stutters once it has. **KV used** is the signal "
        "admission control reads, and **shed** is it acting.\n\n"
        "Healthy looks like: both replicas carrying requests, TTFT under a "
        "couple of seconds, KV climbing under load and recovering after, and "
        "shed at zero until you push it.\n\n"
        "Then: **1 · Gateway** for why a request was refused or routed, "
        "**2 · Engines** for which replica is struggling, **3 · KV** for "
        "pressure and reuse, **4 · Hardware** for the cards."
    )
    return _dash("dodona-overview", "Dodona / 0 · Overview", [
        _text(1, story),

        # --- the four numbers worth a glance ---
        _panel(2, "Time to first token (p95)",
               "histogram_quantile(0.95, sum by (le) "
               "(rate(vllm:time_to_first_token_seconds_bucket[5m]))) * 1000",
               legend="p95", kind="stat", x=0, y=4, w=6, h=4, unit="ms",
               desc="How long a listener waits in silence before the answer "
                    "starts. Prefill plus queueing; the number that decides "
                    "whether the agent feels alive."),
        _panel(3, "Inter-token latency (p95)",
               "histogram_quantile(0.95, sum by (le) "
               "(rate(vllm:inter_token_latency_seconds_bucket[5m]))) * 1000",
               legend="p95", kind="stat", x=6, y=4, w=6, h=4, unit="ms",
               desc="The gap between tokens once generation starts. This is "
                    "what makes synthesised speech stutter — a good TTFT with a "
                    "bad ITL still sounds broken."),
        _panel(4, "In flight",
               "sum(vllm:num_requests_running)", legend="running",
               kind="stat", x=12, y=4, w=6, h=4,
               extra=[("sum(vllm:num_requests_waiting)", "waiting")],
               desc="Requests on a GPU right now, and requests queued behind "
                    "them. Waiting rising while running is flat means the "
                    "engines are at their concurrency limit."),
        _panel(5, "KV used (fullest replica)",
               "max(vllm:kv_cache_usage_perc)", legend="KV",
               kind="stat", x=18, y=4, w=6, h=4, unit="percentunit",
               desc="The signal the admission policy reads. It sheds at 0.90 — "
                    "if this never approaches it under load, the run is INERT "
                    "and proves nothing about the policy."),

        # --- latency, the three views that matter ---
        _panel(6, "Time to first token — p50 / p95 / p99",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(vllm:time_to_first_token_seconds_bucket[5m]))) * 1000",
               legend="p50", x=0, y=8, unit="ms",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:time_to_first_token_seconds_bucket[5m]))) * 1000", "p95"),
                      ("histogram_quantile(0.99, sum by (le) "
                       "(rate(vllm:time_to_first_token_seconds_bucket[5m]))) * 1000", "p99")],
               desc="The spread is the story: a p50 that stays flat while p99 "
                    "climbs is a queue forming, and it shows here before any "
                    "average moves."),
        _panel(7, "Inter-token latency — p50 / p95 / p99",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(vllm:inter_token_latency_seconds_bucket[5m]))) * 1000",
               legend="p50", x=12, y=8, unit="ms",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:inter_token_latency_seconds_bucket[5m]))) * 1000", "p95"),
                      ("histogram_quantile(0.99, sum by (le) "
                       "(rate(vllm:inter_token_latency_seconds_bucket[5m]))) * 1000", "p99")],
               desc="Rises when an engine is batching more work per step — "
                    "better throughput, worse experience. The trade the "
                    "scheduler is making, visible."),

        _panel(8, "End-to-end latency — p50 / p95 / p99",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(vllm:e2e_request_latency_seconds_bucket[5m])))",
               legend="p50", x=0, y=16, unit="s",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:e2e_request_latency_seconds_bucket[5m])))", "p95"),
                      ("histogram_quantile(0.99, sum by (le) "
                       "(rate(vllm:e2e_request_latency_seconds_bucket[5m])))", "p99")],
               desc="A whole request, queue to last token. In SECONDS, unlike "
                    "the panels above — a research answer legitimately takes "
                    "tens of seconds, and milliseconds would be six digits."),
        _panel(9, "Queue time p95 vs prefill p95 vs decode p95",
               "histogram_quantile(0.95, sum by (le) "
               "(rate(vllm:request_queue_time_seconds_bucket[5m]))) * 1000",
               legend="queued", x=12, y=16, unit="ms",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:request_prefill_time_seconds_bucket[5m]))) * 1000", "prefill"),
                      ("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:request_decode_time_seconds_bucket[5m]))) * 1000", "decode")],
               desc="Where the time actually went. Queue time climbing while "
                    "prefill and decode stay flat means the fleet is "
                    "admission-limited, not compute-limited — and more GPU "
                    "would not help."),

        # --- throughput and what is being refused ---
        _panel(10, "Requests: arriving vs completed vs shed",
               "sum(rate(orch_requests_total[1m]))", legend="arrived",
               x=0, y=24, unit="reqps",
               extra=[("sum(rate(orch_completed_total[1m]))", "completed"),
                      ("sum(rate(orch_shed_total[1m]))", "shed (503)")],
               desc="The three lines should sum. A gap between arrived and "
                    "completed+shed is work still in flight; shed above zero is "
                    "admission control doing its job, not a fault."),
        _panel(11, "Tokens per second — prompt vs generated",
               "sum(rate(vllm:prompt_tokens_total[1m]))", legend="prompt (read)",
               x=12, y=24,
               extra=[("sum(rate(vllm:generation_tokens_total[1m]))", "generated (written)")],
               desc="Throughput in the only unit that matters on a GPU. Prompt "
                    "tokens are prefill work, generated tokens are decode work; "
                    "a research agent reads far more than it writes."),

        # --- the fleet, and the cards under it ---
        _panel(12, "KV cache usage, per replica",
               "max by (pod) (vllm:kv_cache_usage_perc)", legend="{{pod}}",
               x=0, y=32, unit="percentunit", maximum=1,
               thresholds=[("green", None), ("orange", 0.75), ("red", 0.90)],
               desc="Per engine, because the policy sheds on the FULLEST one. "
                    "Two lines far apart means load is not being spread."),
        _panel(13, "Replicas healthy, and the admission policy",
               "sum(orch_replica_healthy)", legend="healthy replicas",
               x=12, y=32,
               extra=[("max by (policy) (admitperf_gate_active)", "gate: {{policy}}")],
               desc="How many engines the gateway believes it can route to, and "
                    "which policy is deciding. policy=none means the gate is "
                    "OFF and nothing will ever be shed."),

        _panel(14, "GPU utilisation, per card",
               "max by (gpu, pod) (DCGM_FI_DEV_GPU_UTIL)",
               legend="GPU {{gpu}} · {{pod}}", x=0, y=40, unit="percent",
               maximum=100,
               desc="Share of time the SMs were busy. Low utilisation with high "
                    "KV is the memory-bound decode signature — the case where "
                    "another card buys you nothing."),
        _panel(15, "Preemptions and evictions",
               "sum(rate(vllm:num_preemptions_total[5m]))", legend="preemptions/s",
               x=12, y=40,
               extra=[("sum(rate(orch_kv_evict_total[5m]))", "KV evictions/s")],
               desc="Both are work being thrown away and redone. Any preemption "
                    "means the gate admitted more than the fleet could hold, and "
                    "the engine did admission control after the fact — which is "
                    "the outcome the policy exists to prevent."),
    ], ["overview", "slo"])


def gateway() -> dict:
    """Stage 1: admit, queue, route. Everything here is the gateway's own view.

    Every query aggregates. The gateway process restarts on every
    `make fleet-runpod`, and an unaggregated orch_* query draws one line per
    restart for ever.
    """
    story = (
        "## 1 · Gateway — admit, queue, route\n"
        "A request arrives here first. The gateway **guards** it (known model? "
        "sane `max_tokens`?), **admits or sheds** it against KV pressure, then "
        "**picks a replica**. Nothing below has touched a GPU yet — this is the "
        "decision, and it costs about 90 ms.\n\n"
        "**Routing is load-aware, not capability-aware.** Every engine runs the "
        "same model, so there is no text-vs-vision choice to make: `place()` "
        "scores replicas on `active_requests` and `kv_utilisation` and sends the "
        "request to the least loaded, with a prefix-affinity bonus when one of "
        "them already holds the prompt. Least-loaded is the policy; sticky is "
        "the optimisation on top of it.\n\n"
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
               "(rate(orch_request_duration_seconds_bucket[5m]))) * 1000",
               legend="p95", kind="stat", x=18, y=4, w=6, h=4, unit="ms",
               desc="Time inside the gateway, end to end, including the engine "
                    "it routed to. In MILLISECONDS: the decision itself costs "
                    "about 90ms, and seconds renders that as 0.09 — a number "
                    "nobody can read at a glance or compare across a row."),

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
               "(rate(orch_request_duration_seconds_bucket[5m]))) * 1000",
               legend="p50", x=12, y=8, unit="ms",
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(orch_request_duration_seconds_bucket[5m]))) * 1000", "p95"),
                      ("histogram_quantile(0.99, sum by (le) "
                       "(rate(orch_request_duration_seconds_bucket[5m]))) * 1000", "p99")],
               desc="How long a request spends inside the gateway, end to end, "
                    "including the engine it routed to. The spread between p50 "
                    "and p99 is what a queue looks like before it is a queue."),

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
        _panel(11, "Where the work landed, per replica",
               "sum by (pod) (rate(vllm:request_success_total[5m]))",
               legend="{{pod}}", x=12, y=24, unit="reqps",
               desc="Completed requests per engine — the outcome of every "
                    "routing decision. This replaced a breakdown by capability "
                    "(text vs vision), which answers a question this fleet does "
                    "not ask: one model per card, so the choice is WHICH "
                    "replica, never which kind. Two lines moving together is "
                    "load-aware routing working; one flat at zero is not."),

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
        "## 2 · Engines — one per card, both phases\n"
        "One vLLM per GPU, every one running the same model and serving "
        "**both phases** of every request it is given. There is no prefill "
        "worker and no decode worker here — the router chooses an engine, "
        "and that engine does the whole request.\n\n"
        "The phases are still worth watching, because they load the card "
        "differently and the panels below separate them per request: "
        "**prefill** is one pass over the prompt, compute-bound and bursty; "
        "**decode** is a token at a time, memory-bandwidth-bound and steady, "
        "and it is decode that holds KV for the life of the answer.\n\n"
        "Two engines moving together is `place()` spreading load. One flat "
        "at zero is not.\n\n"
        "Next: **3 · KV**, the pressure admission control reads."
    )
    return _dash("dodona-engine", "Dodona / 2 · Engines — one per card", [
        _text(1, story),

        _panel(2, "Running", "sum(vllm:num_requests_running)", legend="running",
               kind="stat", x=0, y=4, w=6, h=4,
               desc="Requests on a GPU right now, across every engine."),
        _panel(3, "Waiting", "sum(vllm:num_requests_waiting)", legend="waiting",
               kind="stat", x=6, y=4, w=6, h=4,
               desc="Queued inside the engines. Rises before latency does."),
        # Every engine serves both phases, so `phase` is "both" on every
        # series and filtering by it would empty the panel. The question
        # becomes which replica is fullest, which is what admission reads.
        _panel(4, "KV used — fullest engine",
               "max(vllm:kv_cache_usage_perc)", legend="fullest",
               kind="stat", x=12, y=4, w=6, h=4, unit="percentunit",
               desc="The fullest engine's KV cache. The admission policy is "
                    "thresholded at 0.90; if this never approaches it the "
                    "run is INERT — lower SLICE_MIB until it does."),
        _panel(5, "KV used — fleet average",
               "avg(vllm:kv_cache_usage_perc)", legend="average",
               kind="stat", x=18, y=4, w=6, h=4, unit="percentunit",
               desc="Averaged across engines. A wide gap between this and "
                    "the fullest engine means place() is not spreading "
                    "load evenly."),

        _panel(6, "Requests running, per engine",
               "sum by (pod, phase) (vllm:num_requests_running)",
               legend=POD, x=0, y=8,
               desc="Two decode lines moving independently is place() spreading "
                    "load; one flat at zero means it is not."),
        _panel(7, "Requests waiting, per engine",
               "sum by (pod, phase) (vllm:num_requests_waiting)",
               legend=POD, x=12, y=8,
               desc="An engine-side queue, distinct from the gateway's view on "
                    "screen 1."),

        _panel(8, "KV cache usage, per engine",
               "max by (pod, phase) (vllm:kv_cache_usage_perc)",
               legend=POD, x=0, y=16, unit="percentunit",
               maximum=1, thresholds=[("green", None), ("orange", 0.75), ("red", 0.90)],
               desc="The signal the whole project is about. Red is 0.90, where "
                    "the admission policy starts shedding."),
        _panel(9, "Preemptions",
               "sum by (pod, phase) (rate(vllm:num_preemptions_total[5m]))",
               legend=POD, x=12, y=16,
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
                       "(rate(vllm:e2e_request_latency_seconds_bucket[5m])))", "p99")],
               desc="A whole request as the engine measures it: queue plus "
                    "prefill plus every decode step. Compare with the gateway's "
                    "own p95 — the difference is what the control plane costs."),

        _panel(16, "Prompt tokens/s vs generated tokens/s",
               "sum by (phase) (rate(vllm:prompt_tokens_total[1m]))",
               legend="prompt", x=0, y=48,
               extra=[("sum by (phase) (rate(vllm:generation_tokens_total[1m]))",
                       "generated")],
               desc="How fast the fleet reads versus how fast it writes. Prompt "
                    "tokens are prefill work and generated tokens are decode "
                    "work, and the ratio between the lines is the shape of your "
                    "workload — a research agent is prompt-heavy, a chatbot is "
                    "not."),
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
               legend=POD, x=0, y=56, unit="reqps",
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
    """Stage 3: KV pressure and reuse — and, when the pools are split, the hop.

    Read `docs/project/topology.md` before presenting this screen. A real split
    needs three things: placement, orchestration, and transfer. Transfer lives
    INSIDE vLLM, behind `--kv-transfer-config`, because vLLM owns the block
    table — and `infra/setup/engine.sh` cannot pass the manifests' version,
    which names an RDMA device and a cluster DNS name.

    So no KV bytes move on this path in either topology. `KVBus.transfer` writes
    a row and POSTs it to the ledger; `router/nccl.py` and `router/nixl.py` are
    `return`. What differs is whether a hop is even attempted:

      aggregated     one pool, so queue.py short-circuits and there is no hop.
                     Every mooncake_* series is zero BY CONSTRUCTION, so those
                     panels are not built — a screen of flat zeros is exactly
                     the blank-panel failure this file exists to prevent.
      disaggregated  the hop is attempted and recorded as a decision. The
                     panels say "would have moved", because that is the truth.

    What is real in both: KV pressure, which is the signal admission control
    reads, and prefix reuse, which is the saving prefix-aware routing buys.
    """
    story = (
        "## 3 · KV — pressure and reuse\n"
        "Every engine serves both phases here, so there is **no hop**: "
        "`queue.py` short-circuits when the prefill and decode pools are the "
        "same object, and the ledger on `:50051` stays at zero by design. "
        "That is why no hop panels are on this screen — flat zeros teach you "
        "to ignore empty graphs.\n\n"
        "What is on it is real. **KV usage is the signal admission control "
        "reads**: the policy sheds at 0.90, so if the top panels never climb "
        "there the run is INERT and proves nothing — lower `SLICE_MIB` until "
        "they do. **Prefix reuse** is what prefix-aware routing buys: a "
        "request sent back to the engine already holding its prompt skips "
        "the prefill entirely.\n\n"
        "Why no hop: a real split needs placement, orchestration AND "
        "transfer, and transfer lives inside vLLM behind "
        "`--kv-transfer-config`. See `docs/project/topology.md`.\n\n"
        "Next: **4 · Hardware**, the cards all of this runs on."
    )
    hop_panels = [
        _panel(2, "Fleet KV headroom", "min(orch_kv_free_ratio)", legend="free",
               kind="stat", x=0, y=4, w=6, h=4, unit="percentunit",
               desc="Free fraction across the fleet, as the gateway computes "
                    "it. This is what place() and admit both read."),
        _panel(3, "Fullest engine", "max(vllm:kv_cache_usage_perc)",
               legend="KV used", kind="stat", x=6, y=4, w=6, h=4,
               unit="percentunit",
               desc="The engine closest to its KV limit. Admission is "
                    "thresholded at 0.90; this is the number that has to "
                    "reach it for the demo to show anything."),
        _panel(4, "Admission policy",
               "max by (policy) (admitperf_gate_active)", legend="{{policy}}",
               kind="stat", x=12, y=4, w=6, h=4,
               desc="1 while the named policy is deciding. policy=none means "
                    "ADMISSION_POLICY is unset and nothing is being shed."),
        _panel(5, "Shed", "sum(rate(orch_shed_total[1m]))", legend="503/s",
               kind="stat", x=18, y=4, w=6, h=4, unit="reqps",
               desc="Refused for capacity. Non-zero means the KV threshold "
                    "fired — which is the whole point of the fleet."),
    ]
    base_y = 8
    pid = 6

    # Real in both topologies: the pressure admission reads, and the reuse that
    # prefix-aware routing earns. Laid out after whatever the branch above put
    # on the screen.
    shared = [
        _panel(pid, "KV cache usage, per engine",
               "max by (pod) (vllm:kv_cache_usage_perc)", legend="{{pod}}",
               x=0, y=base_y, unit="percentunit", maximum=1,
               thresholds=[("green", None), ("orange", 0.75), ("red", 0.90)],
               desc="The signal the whole project is about, per replica. Red is "
                    "0.90, where the admission policy starts shedding."),
        _panel(pid + 1, "KV headroom per replica",
               "min by (pod, pool) (orch_replica_kv_free_ratio)",
               legend="{{pod}} ({{pool}})", x=12, y=base_y, unit="percentunit",
               maximum=1, thresholds=[("green", None), ("red", 0.10)],
               desc="The same pressure as the gateway sees it, which is what "
                    "place() scores replicas on. Red is under 10% free."),

        _panel(pid + 2, "Preemptions",
               "sum by (pod) (rate(vllm:num_preemptions_total[5m]))",
               legend="{{pod}}", x=0, y=base_y + 8,
               desc="vLLM evicting a running request's KV to make room. Every "
                    "preemption is work thrown away and later redone — this is "
                    "what admission control exists to prevent."),
        _panel(pid + 3, "KV evictions recorded by the gateway",
               "sum(rate(orch_kv_evict_total[5m]))", legend="evictions/s",
               x=12, y=base_y + 8,
               desc="Entries the gateway dropped from its prefix index, so the "
                    "next request with that prefix will be prefilled again."),

        _panel(pid + 4, "Prefill skipped: cached vs total prompt tokens",
               "sum(rate(vllm:prompt_tokens_cached_total[5m]))",
               legend="cached (reused)", x=0, y=base_y + 16,
               extra=[("sum(rate(vllm:prompt_tokens_total[5m]))", "total prompt tokens")],
               desc="Cached tokens had their KV already and were not prefilled "
                    "again. The gap between the lines is real prefill work, and "
                    "closing it is what prefix-aware routing is for."),
        _panel(pid + 5, "Prefix cache hit rate",
               "sum(rate(vllm:prefix_cache_hits_total[5m])) / "
               "clamp_min(sum(rate(vllm:prefix_cache_queries_total[5m])), 1)",
               legend="hit rate", x=12, y=base_y + 16, unit="percentunit",
               maximum=1,
               desc="Prompt prefixes already in an engine's cache, so prefill was "
                    "skipped. clamp_min keeps this at 0 rather than blank when "
                    "there is no traffic."),

        _panel(pid + 6, "Sticky routing — sent back to the replica holding the KV",
               "sum(rate(orch_sticky_total[5m]))", legend="sticky/s",
               x=0, y=base_y + 24, unit="reqps",
               desc="The cheapest outcome available: no hop and no recompute, "
                    "because the KV is already where the request landed. Compare "
                    "with the hit rate beside it."),
        _panel(pid + 7, "KV tokens computed during prefill p50 / p95",
               "histogram_quantile(0.50, sum by (le) "
               "(rate(vllm:request_prefill_kv_computed_tokens_bucket[5m])))",
               legend="p50", x=12, y=base_y + 24,
               extra=[("histogram_quantile(0.95, sum by (le) "
                       "(rate(vllm:request_prefill_kv_computed_tokens_bucket[5m])))",
                       "p95")],
               desc="Per request, how many tokens of KV prefill actually had to "
                    "build. Lower than the prompt length means cache reuse."),
    ]

    title = "Dodona / 3 · KV — pressure and reuse"
    return _dash("dodona-kv", title, [_text(1, story), *hop_panels, *shared],
                 ["kv", "admission", "reuse"])


def hardware() -> dict:
    """Stage 4: the three A100s, each labelled with the worker pinned to it.

    `pod` and `phase` are not DCGM labels — the real exporter has no idea what
    is running on a card. gpu_exporter.py adds them from the mapping workers.sh
    uses to pin workers, which is what makes "is the *prefill* card saturated?"
    a question this screen can answer.
    """
    story = (
        "## 4 · Hardware — the cards\n"
        "Where everything above actually happens. Each card carries the "
        "engine pinned to it, so a panel can say `GPU 1 · engine-1` rather "
        "than `GPU 1` — real DCGM cannot do that, because it has no idea "
        "what is running on a card.\n\n"
        "Read this against **2 · Engines**. Both phases run on every card "
        "here, so the two utilisation lines tell you which one dominates "
        "right now: **compute high, memory bandwidth low** is prefill "
        "working; **memory bandwidth high, compute low** is decode, and no "
        "amount of GPU helps that — which is the argument for admission "
        "control rather than more cards.\n\n"
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

        # Only a comparison when there are two roles to compare. With one pool
        # every card is `phase="both"`, so this would draw a single line under a
        # title promising two — and a panel whose title overstates it is how an
        # audience learns to stop reading titles.
        _panel(14, "Compute vs memory bandwidth, per card",
               "max by (gpu, pod) (DCGM_FI_DEV_GPU_UTIL)",
               legend="compute · GPU {{gpu}}",
               x=0, y=40, unit="percent", maximum=100,
               extra=[("max by (gpu, pod) (DCGM_FI_DEV_MEM_COPY_UTIL)",
                       "memory bw · GPU {{gpu}}")],
               desc="Which phase is dominating a card right now. Prefill is "
                    "compute-bound and decode is memory-bound, and every card "
                    "here does both — so the two lines crossing is the workload "
                    "changing shape, not the router moving it."),
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
    "overview": overview,
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
