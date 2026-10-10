"""Shared helpers for the notebooks: read the cluster, build a PDF.

Imported as `from utils import Cluster, Report` — the notebooks sit in this
directory, so no path juggling.

Two things only:

    Cluster   read-only access to the gateway, the pools and Prometheus
    Report    accumulate findings, then save one PDF into ../reports/

Deliberately stdlib + matplotlib. PDFs are built with matplotlib's own PdfPages
rather than nbconvert, because nbconvert's PDF path needs a LaTeX install and
this has to work on a laptop that has neither.

Nothing here imports the control plane. A measurement tool that shares code
with what it measures can agree with it about something they are both wrong
about.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS = REPO_ROOT / "benchmarking" / "reports"

#: Through the tunnel from a laptop; on the box these are all localhost anyway.
DEFAULTS = {
    "gateway": "http://127.0.0.1:8080",
    "prefill": "http://127.0.0.1:30800",
    "decode": "http://127.0.0.1:30801",
    "webui": "http://127.0.0.1:30030",
    "grafana": "http://127.0.0.1:31495",
}


GRAFANA = "http://127.0.0.1:31495"

#: Dashboards installed by `make observability`, by their Grafana uid slug.
DASHBOARDS = {
    "overview": "dodona-overview", "gateway": "dodona-gateway",
    "router": "dodona-router", "vllm": "dodona-vllm",
    "keda": "dodona-keda", "hami": "dodona-hami",
    "mooncake": "dodona-mooncake", "cluster": "dodona-cluster",
    "errors": "dodona-errors", "replicas": "dodona-replicas",
}


def grafana_explore(promql: str, *, minutes: int = 30, base: str = GRAFANA) -> str:
    """A Grafana Explore URL for one PromQL expression.

    So a report can say "here is the number, and here is it moving over time"
    rather than leaving the reader to reconstruct the query.
    """
    state = {
        "datasource": "Prometheus",
        "queries": [{"refId": "A", "expr": promql}],
        "range": {"from": f"now-{minutes}m", "to": "now"},
    }
    return f"{base}/explore?left={urllib.parse.quote(json.dumps(state))}"


def grafana_dashboard(name: str, *, minutes: int = 30, base: str = GRAFANA) -> str:
    uid = DASHBOARDS.get(name, name)
    return f"{base}/d/{uid}?from=now-{minutes}m&to=now"


def _get(url: str, timeout: float = 5.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError):
        return None


def parse_prometheus(text: str) -> dict[str, float]:
    """Flatten Prometheus text into {name{labels}: value}.

    Keeps the label set in the key so per-pod series stay distinguishable —
    collapsing them is how a fleet-wide average gets mistaken for one worker.
    """
    out: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            key, value = line.rsplit(" ", 1)
            out[key.strip()] = float(value)
        except ValueError:
            continue
    return out


#: Roughly four characters per token for English prose — within a few percent,
#: which is all a prompt-length sweep needs.
CHARS_PER_TOKEN = 4


def filler(tokens: int) -> str:
    """A prompt of approximately `tokens` tokens, for a length sweep."""
    body = ("The cluster serves one model on one card split into slices. "
            "Each slice holds its own KV cache. ")
    return (body * (1 + tokens * CHARS_PER_TOKEN // len(body)))[:tokens * CHARS_PER_TOKEN]


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


def sum_by_name(metrics: dict[str, float], name: str) -> float:
    """Total a metric across its label sets."""
    return sum(v for k, v in metrics.items() if k == name or k.startswith(name + "{"))


@dataclass
class Cluster:
    """Read-only view of a running cluster. Every getter returns None on failure
    rather than raising, so a notebook renders a partial picture instead of
    stopping at the first thing that is down."""

    gateway: str = DEFAULTS["gateway"]
    prefill: str = DEFAULTS["prefill"]
    decode: str = DEFAULTS["decode"]
    #: Why the most recent sample() call lost requests, if it did.
    last_error: str | None = None

    def endpoints(self) -> dict[str, bool]:
        """What answers. Run this first — everything below depends on it."""
        checks = {
            "gateway /metrics": f"{self.gateway}/metrics",
            "gateway /v1/models": f"{self.gateway}/v1/models",
            "prefill /metrics": f"{self.prefill}/metrics",
            "decode /metrics": f"{self.decode}/metrics",
        }
        return {name: _get(url, 3.0) is not None for name, url in checks.items()}

    def gateway_metrics(self) -> dict[str, float]:
        body = _get(f"{self.gateway}/metrics")
        return parse_prometheus(body) if body else {}

    def pool_metrics(self, pool: str) -> dict[str, float]:
        base = self.prefill if pool == "prefill" else self.decode
        body = _get(f"{base}/metrics")
        return parse_prometheus(body) if body else {}

    def served_model(self) -> str | None:
        body = _get(f"{self.gateway}/v1/models")
        if not body:
            return None
        try:
            return (json.loads(body).get("data") or [{}])[0].get("id")
        except (json.JSONDecodeError, AttributeError, IndexError):
            return None

    def chat(self, prompt: str, *, model: str | None = None, max_tokens: int = 16,
             timeout: float = 120.0) -> dict:
        """One completion through the gateway. Returns the parsed body plus
        `_latency_s`, or `{"_error": ...}`."""
        import time

        payload = {
            "model": model or self.served_model() or "text",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{self.gateway}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            body = {"_error": f"HTTP {exc.code}", "_body": exc.read().decode()[:400]}
        except Exception as exc:
            body = {"_error": f"{type(exc).__name__}: {exc}"}
        body["_latency_s"] = round(time.perf_counter() - started, 3)
        return body

    def sample(self, prompt: str, n: int = 10, *, max_tokens: int = 16,
               warmup: int = 1) -> list[float]:
        """Latency of n sequential completions, in seconds.

        `warmup` calls are discarded: the first request through a cold prefix
        cache pays for the whole prompt and would dominate a p95 taken over ten.
        Sequential, not concurrent — this measures latency, not throughput.
        """
        for _ in range(warmup):
            self.chat(prompt, max_tokens=max_tokens)
        out = []
        for _ in range(n):
            r = self.chat(prompt, max_tokens=max_tokens)
            if "_error" in r:
                # Surfaced, not dropped. A sweep that silently skips the lengths
                # that failed draws a flat line and calls it a result.
                self.last_error = f"{r['_error']}: {str(r.get('_body', ''))[:160]}"
                continue
            out.append(r["_latency_s"])
        return out

    def kv_usage(self) -> dict[str, float]:
        """kv_cache_usage_perc per pool. The signal admission reads."""
        out = {}
        for pool in ("prefill", "decode"):
            m = self.pool_metrics(pool)
            for name in ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"):
                hit = [v for k, v in m.items() if k.startswith(name)]
                if hit:
                    out[pool] = hit[0]
                    break
        return out


# ── the report ─────────────────────────────────────────────────────────────

def format_table(headers: list[str], rows: list[list[object]]) -> str:
    """Right-size the columns and align them. Numbers right, text left."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(str(h)), *(len(r[i]) for r in cells)) if cells else len(str(h))
              for i, h in enumerate(headers)]
    numeric = [all(_looks_numeric(r[i]) for r in cells) if cells else False
               for i in range(len(headers))]

    def line(values: list[str]) -> str:
        return "  " + "  ".join(
            v.rjust(widths[i]) if numeric[i] else v.ljust(widths[i])
            for i, v in enumerate(values))

    out = [line([str(h) for h in headers]), "  " + "  ".join("-" * w for w in widths)]
    out += [line(r) for r in cells]
    return "\n".join(out)


def _looks_numeric(s: str) -> bool:
    try:
        float(s.rstrip("%").replace(",", "").replace(" GiB", ""))
        return True
    except ValueError:
        return False


@dataclass
class _Block:
    kind: str
    payload: object
    note: str = ""


@dataclass
class Report:
    """Accumulate findings, then `save()` one PDF into ../reports/.

    Renders with matplotlib so there is no LaTeX dependency. Pages are laid out
    as figures; tables and text are drawn rather than typeset, which is ugly
    enough to be honest and portable enough to always work.
    """

    title: str
    subtitle: str = ""
    blocks: list[_Block] = field(default_factory=list)

    # Every add-method also prints, so the notebook output and the PDF cannot
    # disagree and no cell needs its own formatting. Pass show=False to record
    # silently.

    def heading(self, text: str, *, show: bool = True) -> "Report":
        self.blocks.append(_Block("heading", text))
        if show:
            print(f"\n{text}\n{'=' * len(text)}")
        return self

    def text(self, body: str, *, show: bool = True) -> "Report":
        self.blocks.append(_Block("text", body))
        if show and body:
            print(f"  {body}")
        return self

    def table(self, headers: list[str], rows: list[list[object]], note: str = "",
              *, show: bool = True) -> "Report":
        self.blocks.append(_Block("table", (headers, rows), note))
        if show:
            print(format_table(headers, rows))
            if note:
                print(f"  -- {note}")
        return self

    def verdict(self, ok: bool, text: str, *, show: bool = True) -> "Report":
        """A pass/fail line. The point of a sanity report is the verdicts."""
        self.blocks.append(_Block("verdict", (ok, text)))
        if show:
            print(f"  {'PASS' if ok else 'FAIL'}  {text}")
        return self

    def chart(self, draw: Callable, note: str = "") -> "Report":
        """Escape hatch: `draw(ax)` gets a matplotlib Axes. Prefer bar/line."""
        self.blocks.append(_Block("chart", draw, note)); return self

    # Declarative charts. One definition renders twice — plotly inline so it is
    # interactive in the notebook, matplotlib into the PDF. Writing each chart
    # twice is how the two drift apart.

    def bar(self, title: str, labels: list[str], series: dict[str, list[float]],
            *, ylabel: str = "", log: bool = False, hline: float | None = None,
            note: str = "", show: bool = True) -> "Report":
        spec = {"kind": "bar", "title": title, "labels": labels, "series": series,
                "ylabel": ylabel, "log": log, "hline": hline}
        self.blocks.append(_Block("spec", spec, note))
        if show:
            _show_interactive(spec, note)
        return self

    def line(self, title: str, x: list[float], series: dict[str, list[float]],
             *, xlabel: str = "", ylabel: str = "", hlines: dict[str, float] | None = None,
             note: str = "", show: bool = True) -> "Report":
        spec = {"kind": "line", "title": title, "x": x, "series": series,
                "xlabel": xlabel, "ylabel": ylabel, "hlines": hlines or {}}
        self.blocks.append(_Block("spec", spec, note))
        if show:
            _show_interactive(spec, note)
        return self

    def links(self, rows: list[tuple[str, str]], note: str = "",
              *, show: bool = True) -> "Report":
        """Grafana (or any) URLs. Clickable inline, listed as text in the PDF."""
        self.blocks.append(_Block("links", rows, note))
        if show:
            try:
                from IPython.display import display, Markdown
                display(Markdown("\n".join(f"- [{n}]({u})" for n, u in rows)))
            except ImportError:
                for n, u in rows:
                    print(f"  {n}: {u}")
        return self

    # -- rendering ----------------------------------------------------------

    def _render_text_pages(self, pdf) -> None:
        import matplotlib.pyplot as plt

        lines: list[tuple[str, str]] = []
        for b in self.blocks:
            if b.kind == "heading":
                lines.append(("heading", str(b.payload)))
            elif b.kind == "text":
                lines.extend(("text", ln) for ln in str(b.payload).splitlines() or [""])
            elif b.kind == "verdict":
                ok, msg = b.payload
                lines.append(("pass" if ok else "fail", f"{'PASS' if ok else 'FAIL'}  {msg}"))
            elif b.kind == "links":
                for name, url in b.payload:
                    lines.append(("text", f"  {name}"))
                    lines.append(("mono", f"    {url[:150]}"))
                if b.note:
                    lines.append(("text", b.note))
                lines.append(("text", ""))
            elif b.kind == "table":
                headers, rows = b.payload
                lines.extend(("mono", ln) for ln in format_table(headers, rows).splitlines())
                if b.note:
                    lines.append(("text", b.note))
                lines.append(("text", ""))

        PER_PAGE = 46
        pages = [lines[i:i + PER_PAGE] for i in range(0, len(lines), PER_PAGE)] or [[]]
        for n, page in enumerate(pages):
            fig = plt.figure(figsize=(8.27, 11.69))   # A4 portrait
            y = 0.96
            if n == 0:
                fig.text(0.07, y, self.title, size=17, weight="bold"); y -= 0.035
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                fig.text(0.07, y, f"{self.subtitle}    {stamp}".strip(), size=9, color="#555")
                y -= 0.04
            for kind, body in page:
                if kind == "heading":
                    y -= 0.012
                    fig.text(0.07, y, body, size=12, weight="bold")
                elif kind == "pass":
                    fig.text(0.07, y, body, size=9, color="#157f3b", family="monospace")
                elif kind == "fail":
                    fig.text(0.07, y, body, size=9, color="#c0392b",
                             family="monospace", weight="bold")
                elif kind == "mono":
                    fig.text(0.07, y, body, size=8, family="monospace")
                else:
                    fig.text(0.07, y, body, size=9)
                y -= 0.019
            pdf.savefig(fig); plt.close(fig)

    def _render_charts(self, pdf) -> None:
        import matplotlib.pyplot as plt

        for b in (x for x in self.blocks if x.kind in ("chart", "spec")):
            fig, ax = plt.subplots(figsize=(8.27, 4.5))
            if b.kind == "spec":
                _draw_spec_mpl(ax, b.payload)
            else:
                b.payload(ax)
            if b.note:
                fig.text(0.07, 0.02, b.note, size=8, color="#555")
            fig.tight_layout()
            pdf.savefig(fig); plt.close(fig)

    def save(self, name: str) -> Path:
        """Write ../reports/<name>.pdf and return the path."""
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.backends.backend_pdf import PdfPages

        REPORTS.mkdir(parents=True, exist_ok=True)
        out = REPORTS / (name if name.endswith(".pdf") else f"{name}.pdf")
        with PdfPages(out) as pdf:
            self._render_text_pages(pdf)
            self._render_charts(pdf)
            pdf.infodict()["Title"] = self.title
        return out

    def failures(self) -> list[str]:
        return [msg for b in self.blocks if b.kind == "verdict"
                for ok, msg in [b.payload] if not ok]


# ── chart rendering, from one spec ─────────────────────────────────────────

def _draw_spec_mpl(ax, spec: dict) -> None:
    """Render a chart spec into a matplotlib Axes, for the PDF."""
    if spec["kind"] == "bar":
        labels, series = spec["labels"], spec["series"]
        width = 0.8 / max(len(series), 1)
        for i, (name, values) in enumerate(series.items()):
            offset = (i - (len(series) - 1) / 2) * width
            ax.bar([j + offset for j in range(len(labels))], values, width, label=name)
        ax.set_xticks(range(len(labels))); ax.set_xticklabels(labels)
        if spec["log"]:
            ax.set_yscale("log")
        else:
            ax.set_ylim(bottom=0)
        if len(series) > 1:
            ax.legend(fontsize=8)
    else:
        for name, values in spec["series"].items():
            ax.plot(spec["x"], values, "o-", label=name)
        ax.set_xlabel(spec.get("xlabel", ""))
        ax.set_ylim(bottom=0)
        for name, y in spec.get("hlines", {}).items():
            ax.axhline(y, ls="--", color="#c0392b", label=name)
        if len(spec["series"]) > 1 or spec.get("hlines"):
            ax.legend(fontsize=8)
    if spec.get("hline") is not None:
        ax.axhline(spec["hline"], ls="--", color="#c0392b")
    ax.set_ylabel(spec.get("ylabel", ""))
    ax.set_title(spec["title"])


def _show_interactive(spec: dict, note: str = "") -> None:
    """Interactive in the notebook via plotly; static matplotlib if absent.

    Falling back rather than requiring plotly keeps the notebook runnable on a
    machine that only has matplotlib — which is the one the PDF needs anyway.
    """
    try:
        import plotly.graph_objects as go
    except ImportError:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 3.6))
        _draw_spec_mpl(ax, spec)
        if note:
            fig.text(0.02, -0.04, note, size=8, color="#555")
        plt.show()
        return

    fig = go.Figure()
    if spec["kind"] == "bar":
        for name, values in spec["series"].items():
            fig.add_bar(x=spec["labels"], y=values, name=name)
    else:
        for name, values in spec["series"].items():
            fig.add_scatter(x=spec["x"], y=values, mode="lines+markers", name=name)
        for name, y in spec.get("hlines", {}).items():
            fig.add_hline(y=y, line_dash="dash", line_color="#c0392b",
                          annotation_text=name)
    if spec.get("hline") is not None:
        fig.add_hline(y=spec["hline"], line_dash="dash", line_color="#c0392b")
    fig.update_layout(title=spec["title"], yaxis_title=spec.get("ylabel", ""),
                      xaxis_title=spec.get("xlabel", ""),
                      height=380, margin=dict(l=60, r=20, t=50, b=40),
                      template="plotly_white")
    if spec.get("log"):
        fig.update_yaxes(type="log")
    fig.show()
    if note:
        print(f"  -- {note}")
