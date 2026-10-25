from __future__ import annotations

import json
from pathlib import Path

from observability.dashboards import DASHBOARDS, METRIC_NAMES, write_dashboards

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "infra" / "cluster" / "k8s" / "observability" / "dashboards"

def test_dashboards_cover_each_layer() -> None:
    assert set(DASHBOARDS) == {
        "overview",
        "gateway",
        "router",
        "replicas",
        "vllm",
        "keda",
        "hami",
        "mooncake",
        "cluster",
        "errors",
    }
    write_dashboards(DEST)
    for name, builder in DASHBOARDS.items():
        path = DEST / f"{name}.json"
        payload = json.loads(path.read_text())
        assert payload["uid"].startswith("dodona-")
        assert payload == builder()
        blob = json.dumps(payload)
        for metric in METRIC_NAMES[name]:
            assert metric in blob, f"{name} missing {metric}"


def test_no_panel_queries_a_metric_nothing_exports() -> None:
    """A panel with no possible source trains you to ignore empty panels.

    mooncake_hops_total, mooncake_blocks and mooncake_hop_tokens_total came from
    router/mooncake.py — a Python dict counting what our own router POSTed to
    it, deployed as a pod no setup script applies. Three permanently-zero
    panels, one of them titled "Blocks in store" for a dict length.
    """
    import json

    dead = ("mooncake_hops_total", "mooncake_blocks", "mooncake_hop_tokens_total")
    for path in sorted(DEST.glob("*.json")):
        exprs = " ".join(
            t.get("expr", "")
            for panel in json.loads(path.read_text()).get("panels", [])
            for t in panel.get("targets", []))
        for metric in dead:
            assert metric not in exprs, f"{path.name} queries {metric}, which nothing exports"


def test_rate_wraps_the_range_vector_not_the_aggregation() -> None:
    """rate(sum(x)[5m]) is invalid PromQL and Grafana renders it as empty.

    It has to be sum(rate(x[5m])). An empty panel from a typo is
    indistinguishable from an empty panel from a metric that is genuinely zero,
    which is the whole problem with this dashboard.
    """
    import json
    import re

    for path in sorted(DEST.glob("*.json")):
        for panel in json.loads(path.read_text()).get("panels", []):
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                assert not re.search(r"\b(rate|irate|increase)\s*\(\s*sum", expr), (
                    f"{path.name} / {panel.get('title')!r}: {expr}")


def test_a_panel_title_promising_two_series_plots_two() -> None:
    """"X vs Y" and "X + Y" must have at least two targets.

    The overview had "Picks vs sheds" plotting only picks, and
    "KV transfer + evict" plotting only transfer — while a separate panel
    plotted evicts. So "evict" appeared in two titles and was drawn once.
    """
    import json
    import re

    for path in sorted(DEST.glob("*.json")):
        for panel in json.loads(path.read_text()).get("panels", []):
            title = str(panel.get("title", ""))
            if not re.search(r"\b(vs|versus)\b|\+", title):
                continue
            targets = [t for t in panel.get("targets", []) if t.get("expr")]
            # One target can still draw several series: `by (stage)` with a
            # {{stage}} legend fans out. Two targets, or a templated legend.
            fans_out = any("{{" in t.get("legendFormat", "") for t in targets)
            assert len(targets) >= 2 or fans_out, (
                f"{path.name} / {title!r} promises two series but plots one "
                "with a fixed legend")


def test_counters_are_plotted_as_rates_not_raw() -> None:
    """A _total counter drawn raw is a line that only ever climbs.

    It looks flat on any window short enough to be useful, and it looks
    identical whether traffic is flowing or the cluster is idle. Gauges are
    exempt — orch_kv_free_ratio and orch_tokens_in_flight are instantaneous.
    """
    import json

    gauges = ("orch_kv_free_ratio", "orch_tokens_in_flight",
              "orch_planner_desired_replicas", "orch_replica")
    offenders = []
    for path in sorted(DEST.glob("*.json")):
        for panel in json.loads(path.read_text()).get("panels", []):
            if panel.get("type") == "stat":
                continue           # a stat showing a running total is fine
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if "_total" not in expr or any(g in expr for g in gauges):
                    continue
                if not any(f in expr for f in ("rate(", "irate(", "increase(",
                                               "histogram_quantile")):
                    offenders.append(f"{path.name} / {panel.get('title')}: {expr}")
    assert not offenders, "counters drawn raw:\n  " + "\n  ".join(offenders)


def test_every_gateway_query_aggregates() -> None:
    """orch_* is emitted by a pod that restarts on every `make fleet-lambda`.

    A new pod means a new series, so an unaggregated query draws one line per
    deploy generation — forever. Nine deploys produced nine identical "picks"
    legends, nine orch_kv_free_ratio lines and nine prefill/decode pairs on one
    panel.

    `sum by (phase)` and `max by (pod)` are fine: they keep the label that
    carries meaning and collapse the generations.
    """
    import json
    import re

    offenders = []
    for path in sorted(DEST.glob("*.json")):
        for panel in json.loads(path.read_text()).get("panels", []):
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if "orch_" not in expr:
                    continue
                if not re.match(r"\s*(sum|min|max|avg|count|histogram_quantile)"
                                r"\s*(by\s*\([^)]*\))?\s*\(", expr):
                    offenders.append(f"{path.name} / {panel.get('title')}: {expr}")
    assert not offenders, (
        "gateway metrics drawn per pod generation:\n  " + "\n  ".join(offenders))
