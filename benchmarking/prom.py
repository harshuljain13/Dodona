"""Ask Prometheus and Grafana, instead of re-deriving what they already have.

Scraping a pod's /metrics gives you one instantaneous sample. Sorting ten of
them in Python and calling the result p99 is not a p99 — vLLM publishes
histograms, and `histogram_quantile` over them is the real thing.

Three capabilities, in order of how much they matter:

    Prom.quantiles()    p50/p90/p99 from the histogram buckets
    panel_queries()     the PromQL our Grafana dashboards already use
    Grafana.render()    the actual panel, as a PNG

`panel_queries` is the one worth understanding. The dashboards are generated
from infra/cluster/observability/dashboards.py, so their queries are already
reviewed and already what the cluster is judged by. A notebook that writes its
own PromQL will drift from them; one that reads theirs cannot.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = REPO_ROOT / "infra" / "cluster" / "k8s" / "observability" / "dashboards"

PROM_URL = "http://127.0.0.1:31490"
GRAFANA_URL = "http://127.0.0.1:31495"

#: vLLM's latency histograms, by the name a reader would use.
LATENCY_HISTOGRAMS = {
    "e2e": "vllm:e2e_request_latency_seconds",
    "ttft": "vllm:time_to_first_token_seconds",
    "tpot": "vllm:time_per_output_token_seconds",
    "queue": "vllm:request_queue_time_seconds",
    "prefill": "vllm:request_prefill_time_seconds",
    "decode": "vllm:request_decode_time_seconds",
}


def _get_json(url: str, timeout: float = 20.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


@dataclass
class Prom:
    """A read-only Prometheus client. Returns None rather than raising, so a
    notebook renders a partial picture instead of stopping."""

    base: str = PROM_URL

    def up(self) -> bool:
        return _get_json(f"{self.base}/api/v1/query?query=up") is not None

    def query(self, expr: str) -> list[tuple[dict, float]]:
        """Instant query. [(labels, value)]."""
        data = _get_json(f"{self.base}/api/v1/query?query={urllib.parse.quote(expr)}")
        if not data or data.get("status") != "success":
            return []
        return [(r["metric"], float(r["value"][1]))
                for r in data["data"]["result"] if r.get("value")]

    def query_range(self, expr: str, *, minutes: int = 30,
                    step: str = "15s") -> list[tuple[dict, list[tuple[float, float]]]]:
        """Range query. [(labels, [(unix_ts, value), ...])].

        The whole point of going through Prometheus: one scrape cannot tell a
        steady state from a spike you happened to catch.
        """
        url = (f"{self.base}/api/v1/query_range?query={urllib.parse.quote(expr)}"
               f"&start={_ago(minutes)}&end={_now()}&step={step}")
        data = _get_json(url)
        if not data or data.get("status") != "success":
            return []
        out = []
        for r in data["data"]["result"]:
            points = [(float(ts), float(v)) for ts, v in r.get("values", [])
                      if v not in ("NaN", "+Inf", "-Inf")]
            out.append((r["metric"], points))
        return out

    def quantiles(self, which: str = "e2e", *, qs=(0.5, 0.9, 0.99),
                  window: str = "5m") -> dict[float, float]:
        """p50/p90/p99 over a vLLM latency histogram, in seconds.

        This is the number a report should quote. Taking ten samples and
        sorting them gives a p99 that is just the maximum of ten.
        """
        metric = LATENCY_HISTOGRAMS.get(which, which)
        out: dict[float, float] = {}
        for q in qs:
            expr = (f"histogram_quantile({q}, sum by (le) "
                    f"(rate({metric}_bucket[{window}])))")
            got = self.query(expr)
            # NaN means the rate() window saw no samples — the metric exists but
            # nothing happened recently. Printing it as a number reads as a
            # measurement; dropping it reads as "no data", which is the truth.
            if got and got[0][1] == got[0][1]:
                out[q] = got[0][1]
        return out

    def histogram_stats(self, which: str = "e2e") -> dict[str, float]:
        """count, sum and the true mean from a histogram's _sum and _count.

        The sanity check on a quantile. histogram_quantile interpolates INSIDE
        a bucket, so when the data is concentrated it returns the bucket edge —
        and two different metrics sharing bucket boundaries then report
        identical percentiles while their means differ by 4x. That happened
        here: e2e and queue both read p50 0.15s, while the means were 0.0896s
        and 0.0258s.
        """
        metric = LATENCY_HISTOGRAMS.get(which, which)
        total = self.query(f"sum({metric}_sum)")
        count = self.query(f"sum({metric}_count)")
        if not total or not count or count[0][1] == 0:
            return {}
        return {"count": count[0][1], "sum": total[0][1],
                "mean": total[0][1] / count[0][1]}

    def series_exist(self, prefix: str) -> int:
        """How many series start with this prefix. 0 means nothing scrapes it,
        which is a different problem from a metric that is genuinely zero."""
        data = _get_json(f"{self.base}/api/v1/label/__name__/values")
        if not data:
            return 0
        return sum(1 for n in data.get("data", []) if n.startswith(prefix))


@dataclass
class Grafana:
    """Panel rendering. Needs renderer.yaml deployed, or /render returns 500."""

    base: str = GRAFANA_URL
    user: str = "admin"
    password: str = ""

    def render_url(self, uid: str, panel_id: int, *, minutes: int = 30,
                   width: int = 1000, height: int = 400) -> str:
        return (f"{self.base}/render/d-solo/{uid}/_?orgId=1&panelId={panel_id}"
                f"&from=now-{minutes}m&to=now&width={width}&height={height}")

    def render(self, uid: str, panel_id: int, dest: Path, **kw) -> Path | None:
        """Save the real Grafana panel as a PNG. None if rendering is not set up.

        Worth preferring over a matplotlib lookalike: a screenshot of the panel
        your team already looks at needs no explanation of how it was derived.
        """
        url = self.render_url(uid, panel_id, **kw)
        req = urllib.request.Request(url)
        if self.password:
            import base64
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return None
        if not body.startswith(b"\x89PNG"):
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest


#: What a healthy cluster contains. Checked against kube-state-metrics rather
#: than kubectl, so a notebook needs only the tunnel — and so "deployed" means
#: what the cluster reports, not what a manifest claims.
EXPECTED_DEPLOYMENTS = {
    "vllm-prefill": "builds the prompt's KV",
    "vllm-decode": "generates the answer",
    "orch-serve": "the gateway: guardrails, admission, placement, hop",
    "open-webui": "hand testing, through the gateway",
}

#: Metric families, and who emits each. A family with zero series means nothing
#: scrapes it — a different problem from a metric that is genuinely zero, and
#: indistinguishable on a Grafana panel.
METRIC_FAMILIES = {
    "vllm:": "the engines",
    "orch_": "our gateway",
    "DCGM_FI": "GPU telemetry",
    "kube_": "kube-state-metrics: replicas, restarts",
    "keda_": "the autoscaler",
}


def deployments(prom: "Prom") -> dict[str, tuple[float, float, float]]:
    """{name: (desired, available, restarts)} from kube-state-metrics."""
    def by_deploy(expr: str, key: str) -> dict[str, float]:
        return {m.get(key, "?"): v for m, v in prom.query(expr)}

    desired = by_deploy("kube_deployment_spec_replicas", "deployment")
    available = by_deploy("kube_deployment_status_replicas_available", "deployment")
    restarts = {}
    for m, v in prom.query(
            "sum by (pod) (kube_pod_container_status_restarts_total)"):
        pod = m.get("pod", "")
        for name in EXPECTED_DEPLOYMENTS:
            if pod.startswith(name):
                restarts[name] = restarts.get(name, 0) + v
    return {name: (desired.get(name, 0), available.get(name, 0), restarts.get(name, 0))
            for name in EXPECTED_DEPLOYMENTS}


#: Every metric we rely on, grouped by the QUESTION it answers rather than by
#: its name. Each carries what it cannot tell you, which is the part that keeps
#: being forgotten — every wrong result in this project came from reading a
#: metric as an answer to a question it does not answer.
#:
#: (question, promql, means, blind spot)
CATALOGUE: dict[str, list[tuple[str, str, str, str]]] = {
    "Is the GPU busy?": [
        ("How full is the KV cache?", "max(vllm:kv_cache_usage_perc)",
         "fraction of a pod's own KV in use — the signal kv_threshold reads",
         "it is a RATIO of that slice. 50% of 4.1 GiB is not 50% of the card."),
        ("How many sequences are resident?", "sum(vllm:num_requests_running)",
         "requests the scheduler is stepping right now",
         "says nothing about queued work, which is where latency comes from."),
        ("Is anything queued?", "sum(vllm:num_requests_waiting)",
         "admitted but not yet running — the scheduler is the limiter",
         "zero can mean idle OR that admission is shedding before the queue."),
        ("Did we overcommit?", "sum(vllm:num_preemptions_total)",
         "the engine evicted a running sequence to fit another",
         "a lagging counter: by the time it moves, a user already stalled."),
    ],
    "How fast is it?": [
        ("Dead air before the answer starts",
         "histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))",
         "TTFT p95 — for a voice agent this is silence",
         "a p95 over a 5m window hides a 30s stall that hit three users."),
        ("How it reads aloud",
         "histogram_quantile(0.95, sum by (le) (rate(vllm:time_per_output_token_seconds_bucket[5m])))",
         "ITL — inter-token latency, the cadence of speech",
         "an average ITL can look fine while the gaps are bunched."),
        ("The whole request",
         "histogram_quantile(0.95, sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket[5m])))",
         "E2E p95 — what the caller waits",
         "includes queue time, so it moves when load changes, not just the GPU."),
        ("Time lost before the GPU touched it",
         "histogram_quantile(0.95, sum by (le) (rate(vllm:request_queue_time_seconds_bucket[5m])))",
         "queue p95 — the part admission control is meant to bound",
         "high queue with low KV means the SCHEDULER is binding, not memory."),
    ],
    "Did the KV hop move bytes?": [
        ("Was the connector asked?", "sum(vllm:external_prefix_cache_queries_total)",
         "decode looked for blocks on the producer",
         "being asked is not being answered."),
        ("Did it find blocks?", "sum(vllm:external_prefix_cache_hits_total)",
         "the LOOKUP matched",
         "A LOOKUP, NOT A TRANSFER. The pull can still fail. This exact metric "
         "produced a wrong result here before it was caught."),
        ("Did bytes actually arrive?",
         'sum(vllm:prompt_tokens_by_source_total{source="external_kv_transfer"})',
         "prompt tokens whose KV came over the wire instead of being computed",
         "the honest one. Cross-check against 'Num successful transfers' in the log."),
        ("What did decode compute itself?",
         'sum(vllm:prompt_tokens_by_source_total{source="local_compute"})',
         "prompt work decode could not avoid",
         "nothing — this is the cost the hop exists to remove."),
        ("What came from its OWN cache?",
         'sum(vllm:prompt_tokens_by_source_total{source="local_cache_hit"})',
         "decode serving its own repeat",
         "ONE WORD from the connector metric and constantly mistaken for it. "
         "High here with zero transferred looks like a working hop."),
        ("What did the router record?", "sum(orch_kv_transfer_total)",
         "hops our gateway decided to make",
         "bookkeeping. It counts intent, not bytes, and will happily report "
         "throughput for a transfer that never happened."),
    ],
    "Is admission doing anything?": [
        ("How much arrived?", "sum(orch_requests_total)", "requests past guardrails", ""),
        ("How much was served?", "sum(orch_completed_total)", "completions", ""),
        ("How much was refused?", "sum by (reason, code) (orch_shed_total)",
         "shed, by why",
         "a 429 is the tenant over quota; a 503 is capacity. Reporting them "
         "together makes a quota limit look like a GPU limit."),
        ("Did anything leave?", "sum(orch_overflow_total)",
         "bursted to an external provider",
         "capped for the process lifetime, so 0 can mean 'never needed' or "
         "'budget already spent'."),
    ],
    "Is routing choosing well?": [
        ("How many placements?", "sum(orch_pick_total)", "a pod was chosen", ""),
        ("How many were prefix-warm?", "sum(orch_sticky_total)",
         "placed where the prefix already lives",
         "only meaningful against the total — 10 sticky of 10 is perfect, of "
         "1000 is noise."),
        ("How many were blind?", "sum(orch_pick_unknown_snapshot_total)",
         "chosen with no usable signal",
         "high here means the scorers are inert and placement is effectively "
         "random, whatever the policy says."),
    ],
    "Is the autoscaler awake?": [
        ("What does KEDA see?", "max(keda_scaler_metrics_value)",
         "the value its trigger reads", "it polls — it lags the real signal."),
        ("What has it done?", 'kube_deployment_status_replicas{deployment="vllm-decode"}',
         "actual replicas",
         "scaling UP takes minutes on a pod that loads weights; the metric "
         "moves long before capacity does."),
    ],
}


#: The component checks Part 2 runs. Each is (label, PromQL, what it means).
#: Grouped the way an on-call would read them, not the way the metrics are
#: named — "is the scheduler queueing?" rather than "num_requests_waiting".
COMPONENT_CHECKS: dict[str, list[tuple[str, str, str]]] = {
    "prefill / decode roles": [
        ("prefill prompt tokens", 'sum(vllm:prompt_tokens_total{pod=~"vllm-prefill.*"})',
         "prefill should carry prompt work"),
        ("prefill generated", 'sum(vllm:generation_tokens_total{pod=~"vllm-prefill.*"})',
         "near 0: its leg is sent with max_tokens=1"),
        ("decode generated", 'sum(vllm:generation_tokens_total{pod=~"vllm-decode.*"})',
         "decode does the generating"),
    ],
    "scheduler": [
        ("running", "sum(vllm:num_requests_running)", "in flight on the GPU"),
        ("waiting", "sum(vllm:num_requests_waiting)", "queued behind max-num-seqs"),
        ("preemptions", "sum(vllm:num_preemptions_total)",
         "must be 0 — a preempt is a stall mid-answer"),
        ("KV usage", "max(vllm:kv_cache_usage_perc)", "the signal admission reads"),
    ],
    "KV hop": [
        ("connector asked", "sum(vllm:external_prefix_cache_queries_total)",
         "decode looked for remote blocks"),
        ("connector served", "sum(vllm:external_prefix_cache_hits_total)",
         "blocks found remotely"),
        ("tokens transferred",
         'sum(vllm:prompt_tokens_by_source_total{source="external_kv_transfer"})',
         "KV that arrived instead of being computed"),
        ("tokens recomputed",
         'sum(vllm:prompt_tokens_by_source_total{source="local_compute"})',
         "prompt work decode had to do itself"),
        ("hops recorded", "sum(orch_kv_transfer_total)",
         "router bookkeeping — not proof of bytes"),
    ],
    "admission": [
        ("requests", "sum(orch_requests_total)", "reached the gateway"),
        ("completed", "sum(orch_completed_total)", "served"),
        ("shed", "sum(orch_shed_total)", "refused, by reason below"),
        ("overflow", "sum(orch_overflow_total)", "bursted to a provider"),
    ],
    "routing": [
        ("placements", "sum(orch_pick_total)", "a pod was chosen"),
        ("prefix-sticky", "sum(orch_sticky_total)", "placed on a prefix-warm pod"),
        ("unknown snapshot", "sum(orch_pick_unknown_snapshot_total)",
         "placed blind — the scorer had no signal"),
    ],
    "KEDA": [
        ("scaler value", "max(keda_scaler_metrics_value)", "what KEDA sees"),
        ("decode replicas", 'kube_deployment_status_replicas{deployment="vllm-decode"}',
         "what it has done about it"),
    ],
}


def metric_name(expr: str) -> str:
    """The metric a PromQL expression is built on.

    Derived rather than stored beside the query, so the two cannot disagree.
    Strips the aggregation and the label selector, keeps the _bucket suffix off
    a histogram — `vllm:time_to_first_token_seconds` reads better in a table
    than the histogram_quantile wrapping it.
    """
    import re

    found = re.findall(r"\b((?:vllm:|orch_|kube_|keda_|DCGM_)[A-Za-z0-9_:]+)", expr)
    if not found:
        return expr[:40]
    name = found[0]
    if name.endswith("_bucket"):
        name = name[: -len("_bucket")]
    # Keep the selector when it is what distinguishes one row from another:
    # three catalogue entries are prompt_tokens_by_source_total and differ only
    # by source=, so stripping it makes them identical and useless.
    selector = re.search(re.escape(found[0]) + r"\{([^}]*)\}", expr)
    if selector and selector.group(1).strip():
        return f"{name}{{{selector.group(1).strip()}}}"
    return name


def panel_queries(dashboard: str) -> dict[str, list[str]]:
    """{panel title: [PromQL, ...]} from a dashboard we actually deploy.

    So a notebook plots what Grafana plots. Writing the query twice is how the
    two end up disagreeing and nobody notices which is wrong.
    """
    path = DASHBOARDS / f"{dashboard}.json"
    if not path.exists():
        return {}
    spec = json.loads(path.read_text())
    out: dict[str, list[str]] = {}
    for panel in spec.get("panels", []):
        exprs = [t.get("expr", "") for t in panel.get("targets", []) if t.get("expr")]
        if exprs:
            out[panel.get("title", f"panel {panel.get('id')}")] = exprs
    return out


def panel_id(dashboard: str, title_contains: str) -> int | None:
    """The panel id behind a title, for Grafana.render()."""
    path = DASHBOARDS / f"{dashboard}.json"
    if not path.exists():
        return None
    for panel in json.loads(path.read_text()).get("panels", []):
        if title_contains.lower() in str(panel.get("title", "")).lower():
            return panel.get("id")
    return None


def _now() -> int:
    import time
    return int(time.time())


def _ago(minutes: int) -> int:
    return _now() - minutes * 60
