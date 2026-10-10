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
