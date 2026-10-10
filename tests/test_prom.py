"""prom.py is how notebooks stop re-deriving what Prometheus already has."""
from __future__ import annotations

import re

from prom import LATENCY_HISTOGRAMS, Grafana, Prom, panel_id, panel_queries


def test_dashboard_queries_are_readable_from_the_json_we_deploy() -> None:
    """The point of the module: plot what Grafana plots, from one source."""
    queries = panel_queries("vllm")
    assert len(queries) >= 8, f"only found {len(queries)} panels"
    joined = " ".join(e for exprs in queries.values() for e in exprs)
    assert "vllm:" in joined
    assert panel_queries("does-not-exist") == {}


def test_panel_id_resolves_a_title_to_something_renderable() -> None:
    assert isinstance(panel_id("vllm", "ttft"), int)
    assert panel_id("mooncake", "delivered") is not None
    assert panel_id("vllm", "no such panel") is None


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
