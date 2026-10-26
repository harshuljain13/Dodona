"""prom.py is how notebooks stop re-deriving what Prometheus already has."""
from __future__ import annotations

import re

from prom import LATENCY_HISTOGRAMS, Grafana, Prom, panel_id, panel_queries


def test_dashboard_queries_are_readable_from_the_dashboards_we_deploy() -> None:
    """The point of the module: plot what Grafana plots, from one source.

    `engine`, not `vllm`: the dashboards are the four this branch provisions,
    in request order, and they are built by calling the generator rather than
    read from disk — they are generated on the box at fleet start and not
    checked in, so a file read found nothing and every lookup returned None.
    """
    queries = panel_queries("engine")
    assert len(queries) >= 8, f"only found {len(queries)} panels"
    joined = " ".join(e for exprs in queries.values() for e in exprs)
    assert "vllm:" in joined
    assert panel_queries("does-not-exist") == {}


def test_panel_id_resolves_a_title_to_something_renderable() -> None:
    """Matched on the words a reader would use, against real panel titles.

    Not "ttft": the panel is called "Time to first token", because a dashboard
    an audience reads should not be written in metric names.
    """
    assert isinstance(panel_id("engine", "time to first token"), int)
    assert panel_id("gateway", "shed") is not None
    assert panel_id("kv", "prefix") is not None
    assert panel_id("engine", "no such panel") is None


def test_quantiles_use_histogram_buckets_not_sorted_samples() -> None:
    """A p99 from ten sorted samples is the maximum of ten.

    Asserted by inspecting the expression, since this runs with no cluster.
    """
    import inspect

    src = inspect.getsource(Prom.quantiles)
    assert "histogram_quantile" in src
    assert "_bucket[" in src
    assert "sum by (le)" in src


def test_every_named_histogram_is_a_real_vllm_metric() -> None:
    for name, metric in LATENCY_HISTOGRAMS.items():
        assert metric.startswith("vllm:"), name
        assert not metric.endswith("_bucket"), f"{name}: add _bucket at use, not here"


def test_the_client_never_raises_when_nothing_answers() -> None:
    """A notebook should render a partial picture, not stop at the first
    thing that is down. Port 1 answers nothing."""
    p = Prom(base="http://127.0.0.1:1")
    assert p.up() is False
    assert p.query("up") == []
    assert p.query_range("up") == []
    assert p.quantiles("ttft") == {}
    assert p.series_exist("vllm:") == 0


def test_render_url_is_the_solo_panel_endpoint() -> None:
    url = Grafana().render_url("dodona-vllm", 4, minutes=15)
    assert "/render/d-solo/dodona-vllm" in url
    assert "panelId=4" in url and "from=now-15m" in url


def test_no_dashboard_query_uses_rate_of_an_aggregation() -> None:
    """rate(sum(x)[5m]) is invalid and Grafana draws it as an empty panel,
    which looks exactly like a metric that is genuinely zero."""
    for name in ("vllm", "mooncake", "gateway", "router"):
        for title, exprs in panel_queries(name).items():
            for expr in exprs:
                assert not re.search(r"\b(rate|irate|increase)\s*\(\s*sum", expr), \
                    f"{name} / {title}: {expr}"


def test_every_catalogue_entry_resolves_to_a_distinct_metric() -> None:
    """The table shows the metric name, so two rows sharing one is useless.

    Three entries are prompt_tokens_by_source_total and differ only by the
    source= label; stripping the selector collapsed them into one.
    """
    from prom import CATALOGUE, metric_name

    seen: dict[str, list[str]] = {}
    for entries in CATALOGUE.values():
        for label, expr, _, _ in entries:
            seen.setdefault(metric_name(expr), []).append(label)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, f"same metric name for different questions: {dupes}"


def test_catalogue_entries_name_a_real_metric_family() -> None:
    from prom import CATALOGUE, metric_name

    for question, entries in CATALOGUE.items():
        for label, expr, means, _ in entries:
            name = metric_name(expr)
            assert name.startswith(("vllm:", "orch_", "kube_", "keda_", "DCGM_")), \
                f"{question} / {label}: {name}"
            assert means, f"{question} / {label} has no explanation"


def test_most_metrics_document_what_they_cannot_tell_you() -> None:
    """The blind-spot column is the point of the catalogue.

    Every wrong result in this project came from reading a metric as the answer
    to a question it does not answer.
    """
    from prom import CATALOGUE

    total = sum(len(v) for v in CATALOGUE.values())
    with_blind = sum(1 for v in CATALOGUE.values() for row in v if row[3])
    assert with_blind >= total * 0.8, f"only {with_blind}/{total} name a blind spot"
