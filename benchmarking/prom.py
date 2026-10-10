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
            if got:
                out[q] = got[0][1]
        return out

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
