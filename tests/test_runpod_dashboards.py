"""The process path's four dashboards, checked against what it actually exports.

The k8s set is guarded by test_dashboards.py. This is the same job for
infra/runpod-cluster, and it exists because the failure it catches already
happened: eleven dashboards were put in front of an audience and four of them
queried `kube_*` on a box with no Kubernetes, so they were permanently blank.

The rule enforced here is the strong version of that lesson — not "no k8s
metrics" but "nothing outside `EXPORTED`", which is the metric list read off a
live fleet's Prometheus.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / "infra" / "runpod-cluster" / "observability"
sys.path.insert(0, str(OBS))

from dashboards import (  # noqa: E402
    DASHBOARDS,
    EXPORTED,
    SPLIT,
    STORY_TAG,
    write_dashboards,
)

#: Every metric family the fleet exports, suffix-stripped, as one set.
FAMILIES = {m for group in EXPORTED.values() for m in group}

#: PromQL keywords, functions and label names that look like metrics to a regex.
NOT_A_METRIC = {
    "sum", "min", "max", "avg", "count", "rate", "irate", "increase", "by",
    "without", "histogram_quantile", "clamp_min", "clamp_max", "le", "pod",
    "pool", "phase", "gpu", "capability", "reason", "policy", "code", "job",
    "instance", "model_name", "engine", "and", "or", "unless", "on",
    "group_left", "group_right", "topk", "bottomk", "quantile", "absent",
    "decode", "prefill",
}


def _dashboards() -> dict[str, dict]:
    return {name: builder() for name, builder in DASHBOARDS.items()}


def _exprs(payload: dict) -> list[tuple[str, str]]:
    """(panel title, expr) for every target on the dashboard."""
    return [(panel.get("title", ""), t.get("expr", ""))
            for panel in payload.get("panels", [])
            for t in panel.get("targets", [])]


def test_the_screens_are_the_request_path() -> None:
    """Overview first, then the path a request travels through it."""
    assert set(DASHBOARDS) == {"overview", "gateway", "engine", "kv", "hardware"}
    for name, payload in _dashboards().items():
        assert payload["uid"] == f"dodona-{name}", (
            f"{name}: the uid must be dodona-<filename>, because workers.sh "
            f"prunes stale dashboards by matching uid against filename")
        assert STORY_TAG in payload["tags"], f"{name} is not linked into the path"
        assert payload["links"], f"{name} has no link to the next screen"
        assert any(p.get("type") == "text" for p in payload["panels"]), (
            f"{name} has no text panel saying where in the path it sits")


def test_no_panel_queries_a_metric_the_fleet_does_not_export() -> None:
    """The blank-panel rule. A panel with no possible source is worse than none.

    It is indistinguishable from a panel whose metric is genuinely zero, so it
    teaches the audience to discount empty panels — including the ones that
    are empty because admission control is working.
    """
    offenders = []
    for name, payload in _dashboards().items():
        for title, expr in _exprs(payload):
            for token in re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", expr):
                if token in NOT_A_METRIC or "_" not in token and ":" not in token:
                    continue
                family = re.sub(r"_(bucket|sum|count)$", "", token)
                if family not in FAMILIES:
                    offenders.append(f"{name} / {title!r}: {token}")
    assert not offenders, (
        "panels querying metrics nothing exports:\n  " + "\n  ".join(offenders))


def test_counters_are_plotted_as_rates() -> None:
    """A _total counter drawn raw only ever climbs, whatever the traffic does."""
    gauges = ("orch_tokens_in_flight", "orch_kv_free_ratio", "orch_replica",
              "orch_planner_desired_replicas", "mooncake_blocks")
    offenders = []
    for name, payload in _dashboards().items():
        for panel in payload["panels"]:
            if panel.get("type") == "stat":
                continue      # a stat showing a running total is the point
            for t in panel.get("targets", []):
                expr = t.get("expr", "")
                if "_total" not in expr or any(g in expr for g in gauges):
                    continue
                if not any(f in expr for f in ("rate(", "irate(", "increase(",
                                               "histogram_quantile")):
                    offenders.append(f"{name} / {panel.get('title')}: {expr}")
    assert not offenders, "counters drawn raw:\n  " + "\n  ".join(offenders)


def test_rate_wraps_the_range_vector_not_the_aggregation() -> None:
    """rate(sum(x)[5m]) is invalid PromQL and Grafana renders it as empty."""
    for name, payload in _dashboards().items():
        for title, expr in _exprs(payload):
            assert not re.search(r"\b(rate|irate|increase)\s*\(\s*sum", expr), (
                f"{name} / {title!r}: {expr}")


def test_every_query_aggregates() -> None:
    """Both orch_* and vllm:* series are re-created whenever a process restarts.

    `make fleet-runpod` restarts all of them, so an unaggregated query draws one
    line per run of the workshop.
    """
    offenders = []
    for name, payload in _dashboards().items():
        for title, expr in _exprs(payload):
            if not expr:
                continue
            if not re.match(r"\s*(sum|min|max|avg|count|histogram_quantile)"
                            r"\s*(by\s*\([^)]*\))?\s*\(", expr):
                offenders.append(f"{name} / {title!r}: {expr}")
    assert not offenders, "unaggregated queries:\n  " + "\n  ".join(offenders)


def test_a_panel_title_promising_two_series_plots_two() -> None:
    """"X vs Y" has to draw two lines — but one target can already do that.

    `max by (phase) (vllm:kv_cache_usage_perc)` is a single target that plots
    prefill and decode, which is exactly the comparison the title promises. So
    the rule is two *series*: either two targets, or one that splits by a label.
    """
    for name, payload in _dashboards().items():
        for panel in payload["panels"]:
            title = panel.get("title", "")
            if " vs " not in title and " + " not in title:
                continue
            targets = panel.get("targets", [])
            splits = any(re.search(r"by\s*\([^)]*\b(phase|pod|pool|gpu)\b",
                                   t.get("expr", "")) for t in targets)
            assert len(targets) >= 2 or splits, (
                f"{name} / {title!r} promises a comparison but plots one series")


def test_panel_ids_are_unique_within_a_dashboard() -> None:
    """Grafana silently drops the second panel sharing an id."""
    for name, payload in _dashboards().items():
        ids = [p["id"] for p in payload["panels"]]
        assert len(ids) == len(set(ids)), f"{name} has duplicate panel ids"


def test_a_ratio_panel_cannot_divide_by_zero_into_a_blank() -> None:
    """`a / b` with no traffic is 0/0 = NaN, which Grafana draws as nothing.

    clamp_min on the denominator makes an idle fleet read 0 — which is the true
    answer and, more importantly, a visible one.
    """
    for name, payload in _dashboards().items():
        for title, expr in _exprs(payload):
            # A bare "/ 1024" style unit conversion is not a ratio of metrics.
            if re.search(r"\)\s*/\s*(sum|min|max|avg|count|clamp_min)", expr):
                assert "clamp_min" in expr, (
                    f"{name} / {title!r} divides metric by metric without "
                    f"clamp_min, so it is blank when idle: {expr}")


def test_percentunit_panels_are_actually_fractions() -> None:
    """A 0-100 metric with unit percentunit renders as 5500%."""
    for name, payload in _dashboards().items():
        for panel in payload["panels"]:
            unit = panel.get("fieldConfig", {}).get("defaults", {}).get("unit")
            if unit != "percentunit":
                continue
            for t in panel.get("targets", []):
                expr = t.get("expr", "")
                assert "DCGM_FI_DEV_GPU_UTIL" not in expr, (
                    f"{name} / {panel.get('title')!r}: DCGM utilisation is 0-100, "
                    f"not a fraction — use unit=percent")


def test_written_json_matches_the_builders(tmp_path: Path) -> None:
    written = write_dashboards(tmp_path)
    assert len(written) == len(DASHBOARDS)
    for path in written:
        payload = json.loads(path.read_text())
        assert payload == DASHBOARDS[path.stem]()


@pytest.mark.parametrize("name", sorted(DASHBOARDS))
def test_every_exported_group_is_represented(name: str) -> None:
    """Each screen must actually use the component it is named after.

    Catches a dashboard that is all framing text and no data.

    `kv` is the exception, and deliberately: in the aggregated topology there is
    no hop, so every `mooncake_*` series is zero by construction and the panels
    are not built. What the screen must still show is the KV pressure admission
    reads — so the requirement is `vllm:kv_cache_usage_perc`, not the ledger.
    """
    blob = json.dumps(DASHBOARDS[name]())
    expected = {
        # Overview is the one screen that spans everything, so it is required to
        # carry the number a listener actually feels rather than any one
        # component's metrics.
        "overview": "vllm:time_to_first_token_seconds",
        "gateway": "orch_",
        "engine": "vllm:",
        "kv": "mooncake_" if SPLIT else "vllm:kv_cache_usage_perc",
        "hardware": "DCGM_FI_DEV_",
    }[name]
    assert expected in blob, f"{name} never queries {expected}*"


def test_the_ledger_is_not_plotted_when_it_cannot_move() -> None:
    """In `aggregated` there is no hop, so mooncake_* must not be on a panel.

    `queue.py` short-circuits when the prefill and decode pools are the same
    object — `handoff.decode is handoff.prefill` — so the bus is never called
    and every ledger series stays at zero for the life of the fleet. Plotting
    them would be four flat lines, which is the blank-panel failure this file
    exists to prevent, in its most convincing disguise: the metric exists, the
    target is up, and the graph is still meaningless.
    """
    exprs = " ".join(e for _, e in _exprs(DASHBOARDS["kv"]()))
    if SPLIT:
        assert "mooncake_" in exprs, "a split fleet should show the hop it attempts"
    else:
        assert "mooncake_" not in exprs, (
            "aggregated fleets make no hops, so the ledger is flat zero by "
            "construction and must not be given a panel")


def test_the_kv_dashboard_claim_matches_the_engine_config() -> None:
    """The KV screen must not claim a transfer the engine cannot perform.

    This is the project's central claim, and it is the one place a dashboard
    could mislead an audience. A real split needs placement, orchestration AND
    transfer; transfer lives inside vLLM, behind `--kv-transfer-config`, because
    vLLM owns the block table. `KVBus.transfer` writes a ledger row, and
    `router/nccl.py` and `router/nixl.py` are `return` — so without that flag
    decode re-prefills the whole prompt and no bytes move.

    The assertion is tied to engine.sh rather than hardcoded, so it cuts both
    ways: add a connector and this test tells you the dashboard now understates
    what the fleet does. See docs/project/topology.md.
    """
    engine = (ROOT / "infra" / "setup" / "engine.sh").read_text()
    start = engine.index("exec vllm serve")
    has_connector = "--kv-transfer-config" in engine[start:]

    text = " ".join(
        p["options"]["content"]
        for p in DASHBOARDS["kv"]()["panels"]
        if p.get("type") == "text"
    ).lower()

    if not has_connector:
        # Either explanation is honest; which one applies depends on topology.
        # Split pools attempt a hop and decode re-prefills; one pool never
        # attempts one at all.
        assert "re-prefill" in text or "no hop" in text, (
            "engine.sh passes no --kv-transfer-config, so no KV bytes move — "
            "the KV dashboard has to say why, either because decode re-prefills "
            "or because there is no hop to make")
        for claim in ("paying for itself", "transferred rather than recomputed"):
            assert claim not in text, f"the KV screen claims {claim!r} with no connector"
    else:
        assert "re-prefill" not in text or "no connector" not in text, (
            "a connector is configured now — the KV dashboard still describes the "
            "ledger-only path and understates what the fleet does")
