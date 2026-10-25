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
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import sys

BENCHMARKING = Path(__file__).resolve().parent
REPO_ROOT = BENCHMARKING.parent
# slos.py sits next to this file; notebooks live one folder deeper.
sys.path.insert(0, str(BENCHMARKING))


def reports_dir() -> Path:
    """Where this notebook's artefacts go: ./reports/, beside the notebook.

    Each experiment owns its outputs, so a folder is self-contained — the
    notebook, its report, its screenshots. A shared reports/ at the top makes
    you cross-reference filenames to work out which run produced what.
    """
    return Path.cwd() / "reports"


REPORTS = reports_dir()

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
               warmup: int = 1, unique: bool = False) -> list[float]:
        """Latency of n sequential completions, in seconds.

        `warmup` calls are discarded: the first request through a cold prefix
        cache pays for the whole prompt and would dominate a p95 taken over ten.
        Sequential, not concurrent — this measures latency, not throughput.

        `unique` prefixes every call with a different nonce, so vLLM's prefix
        cache cannot serve the repeat. Required for anything that claims to
        measure prompt-processing cost: with a shared prompt, calls 2..n are
        cache hits and the result is flat whether or not KV ever moved.
        """
        def body(i: int) -> str:
            return f"[{i}] {prompt}" if unique else prompt

        for i in range(warmup):
            self.chat(body(-1 - i), max_tokens=max_tokens)
        out = []
        for i in range(n):
            r = self.chat(body(i), max_tokens=max_tokens)
            if "_error" in r:
                # Surfaced, not dropped. A sweep that silently skips the lengths
                # that failed draws a flat line and calls it a result.
                self.last_error = f"{r['_error']}: {str(r.get('_body', ''))[:160]}"
                continue
            out.append(r["_latency_s"])
        return out

    def kv_transfer(self, pool: str = "decode") -> dict[str, float]:
        """Whether the KV connector has ever delivered a block.

        vLLM counts these itself, which makes the hop question a measurement
        rather than a reading of startup logs:

            external_prefix_cache_queries_total   the connector was asked
            external_prefix_cache_hits_total      it returned blocks
            prompt_tokens_by_source{external_kv_transfer}
                                                  tokens that arrived as KV

        queries > 0 with hits == 0 is the unambiguous case: the connector is
        wired up, is being consulted, and is moving nothing.
        """
        m = self.pool_metrics(pool)

        def by_source(source: str) -> float:
            return sum(v for k, v in m.items()
                       if k.startswith("vllm:prompt_tokens_by_source_total")
                       and f'source="{source}"' in k)

        return {
            "queries": sum_by_name(m, "vllm:external_prefix_cache_queries_total"),
            "hits": sum_by_name(m, "vllm:external_prefix_cache_hits_total"),
            # The same prompt tokens, split by where their KV came from. Decode
            # is SENT the whole prompt either way, so prompt_tokens_total never
            # moves — only this split does.
            "computed": by_source("local_compute"),
            "local_cache": by_source("local_cache_hit"),
            "tokens_from_transfer": by_source("external_kv_transfer"),
            "local_hits": sum_by_name(m, "vllm:prefix_cache_hits_total"),
            "local_queries": sum_by_name(m, "vllm:prefix_cache_queries_total"),
        }

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

    def slo(self, key: str, measured: float, *, note: str = "",
            show: bool = True) -> "Report":
        """Check a measurement against the promise in slos.py.

        Records as a verdict, so 99-report can aggregate every SLO check across
        every notebook without each one inventing its own threshold.
        """
        from slos import SLOS

        s = SLOS[key]
        ok = s.met(measured)
        self.blocks.append(_Block("slo", (ok, s.label, s.describe(measured), s.why)))
        if show:
            print(f"  {'MET ' if ok else 'MISS'}  {s.label:<28} {s.describe(measured)}")
            if not ok:
                print(f"        why it matters: {s.why}")
        if note:
            self.text(note, show=show)
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
            stacked: bool = False, horizontal: bool = False, total: bool = False,
            note: str = "", conclusion: str = "", show: bool = True) -> "Report":
        """`stacked` for parts of a whole, `horizontal` when labels are prose.

        Grouped bars ask the reader to add the segments up. If the series sum to
        something meaningful — percentages of one prompt, or the two halves of
        one wait — stacking makes the composition the chart rather than an
        inference from it.
        """
        spec = {"kind": "bar", "title": title, "labels": labels, "series": series,
                "ylabel": ylabel, "log": log, "hline": hline,
                "stacked": stacked, "horizontal": horizontal, "total": total}
        self.blocks.append(_Block("spec", spec, note))
        if show:
            _show_interactive(spec, note)
            if note:
                print(f"  how to read it: {note}")
        if conclusion:
            self.conclusion(conclusion, show=show)
        return self

    def line(self, title: str, x: list[float], series: dict[str, list[float]],
             *, xlabel: str = "", ylabel: str = "", hlines: dict[str, float] | None = None,
             note: str = "", conclusion: str = "", show: bool = True) -> "Report":
        spec = {"kind": "line", "title": title, "x": x, "series": series,
                "xlabel": xlabel, "ylabel": ylabel, "hlines": hlines or {}}
        self.blocks.append(_Block("spec", spec, note))
        if show:
            _show_interactive(spec, note)
            if note:
                print(f"  how to read it: {note}")
        if conclusion:
            self.conclusion(conclusion, show=show)
        return self

    def conclusion(self, text: str, *, show: bool = True) -> "Report":
        """What the measurement above turned out to mean.

        Stated separately from the chart because a chart without a sentence is
        left to the reader, and a reader who has to interpret it will interpret
        it the way they already expected.
        """
        self.blocks.append(_Block("conclusion", text))
        if show:
            print(f"\n  >> {text}\n")
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
            elif b.kind == "conclusion":
                lines.append(("text", ""))
                for part in _wrap(str(b.payload), 92):
                    lines.append(("bold", f">> {part}" if part == _wrap(str(b.payload), 92)[0]
                                  else f"   {part}"))
                lines.append(("text", ""))
            elif b.kind == "slo":
                ok, label, detail, why = b.payload
                lines.append(("pass" if ok else "fail",
                              f"{'MET ' if ok else 'MISS'}  {label:<28} {detail}"))
                if not ok:
                    lines.append(("text", f"      {why}"))
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
                elif kind == "bold":
                    fig.text(0.07, y, body, size=9.5, weight="bold", color="#0b3d91")
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

        target = reports_dir()
        target.mkdir(parents=True, exist_ok=True)
        out = target / (name if name.endswith(".pdf") else f"{name}.pdf")
        with PdfPages(out) as pdf:
            self._render_text_pages(pdf)
            self._render_charts(pdf)
            pdf.infodict()["Title"] = self.title
        return out

    def failures(self) -> list[str]:
        """Everything that did not pass — plain verdicts and missed SLOs."""
        out = []
        for b in self.blocks:
            if b.kind == "verdict" and not b.payload[0]:
                out.append(b.payload[1])
            elif b.kind == "slo" and not b.payload[0]:
                out.append(f"SLO MISSED — {b.payload[1]}: {b.payload[2]}")
        return out

    def objective(self, question: str, *, slos: list[str] = (),
                  falsified_by: str = "", show: bool = True) -> "Report":
        """Open every notebook with this: the question, the promise it touches,
        and what result would prove the claim wrong. A notebook without a
        falsifier is a demo."""
        from slos import SLOS

        self.heading("Objective", show=show)
        self.text(question, show=show)
        if slos:
            self.table(["SLO under test", "target"],
                       [[SLOS[k].label, f"{SLOS[k].target:g}{SLOS[k].unit}"] for k in slos],
                       show=show)
        if falsified_by:
            self.text(f"Falsified by: {falsified_by}", show=show)
        return self


# ── chart rendering, from one spec ─────────────────────────────────────────

#: One palette, used in order, so the same series keeps its colour between
#: charts in a notebook. matplotlib's default cycle is fine but low-contrast
#: when printed.
PALETTE = ("#1f6feb", "#d1700a", "#1a7f37", "#a40e26", "#6639ba", "#0d7680")
MARKERS = ("o", "s", "^", "D", "v", "P")


def _draw_spec_mpl(ax, spec: dict) -> None:
    """Render a chart spec into a matplotlib Axes.

    Opinionated about three things a benchmarking chart is unreadable without:
    a grid behind the data so a value can be read off the axis, a legend
    whenever anything is named, and value labels on bars so the reader does not
    have to estimate from the axis.
    """
    labelled = False

    if spec["kind"] == "bar":
        labels, series = spec["labels"], spec["series"]
        horizontal = spec.get("horizontal", False)
        stacked = spec.get("stacked", False)
        pos = list(range(len(labels)))

        if stacked:
            # Parts of a whole. The segment IS the composition; grouped bars
            # would make the reader add them up by eye.
            running = [0.0] * len(labels)
            for i, (name, values) in enumerate(series.items()):
                colour = PALETTE[i % len(PALETTE)]
                if horizontal:
                    ax.barh(pos, values, 0.62, left=running, label=name,
                            color=colour, edgecolor="white", linewidth=0.6, zorder=3)
                else:
                    ax.bar(pos, values, 0.62, bottom=running, label=name,
                           color=colour, edgecolor="white", linewidth=0.6, zorder=3)
                # Only label a segment big enough to hold the text.
                span = max((a + b for a, b in zip(running, values)), default=1) or 1
                for j, val in enumerate(values):
                    if val > span * 0.08:
                        mid = running[j] + val / 2
                        xy = (mid, pos[j]) if horizontal else (pos[j], mid)
                        ax.annotate(_seg_fmt(val), xy, ha="center", va="center",
                                    fontsize=8, color="white", fontweight="bold",
                                    zorder=4)
                running = [a + b for a, b in zip(running, values)]
                labelled = True
            if spec.get("total"):
                for j, tot in enumerate(running):
                    xy = (tot, pos[j]) if horizontal else (pos[j], tot)
                    off = (4, 0) if horizontal else (0, 4)
                    ax.annotate(_seg_fmt(tot), xy, textcoords="offset points",
                                xytext=off, ha="left" if horizontal else "center",
                                va="center" if horizontal else "bottom",
                                fontsize=8, fontweight="bold", color="#30363d")
            top = max(running, default=0)
        else:
            width = 0.8 / max(len(series), 1)
            for i, (name, values) in enumerate(series.items()):
                offset = (i - (len(series) - 1) / 2) * width
                at = [j + offset for j in pos]
                draw = ax.barh if horizontal else ax.bar
                bars = draw(at, values, width, label=name,
                            color=PALETTE[i % len(PALETTE)],
                            edgecolor="white", linewidth=0.6, zorder=3)
                labelled = True
                if not spec.get("log"):
                    ax.bar_label(bars, fmt=_bar_fmt(values), fontsize=8,
                                 padding=2, color="#30363d")
            top = max((v for vs in series.values() for v in vs), default=0)

        if horizontal:
            ax.set_yticks(pos); ax.set_yticklabels(labels, fontsize=9)
            ax.invert_yaxis()      # first case at the top, as the table reads
            ax.set_xlim(0, top * 1.12 if top > 0 else 1)
        else:
            ax.set_xticks(pos)
            ax.set_xticklabels(labels, fontsize=9,
                               rotation=20 if max(map(len, labels)) > 12 else 0,
                               ha="right" if max(map(len, labels)) > 12 else "center")
            # Log needs a positive value somewhere. A freshly restarted fleet
            # reads all zeros, and matplotlib warns rather than falling back.
            if spec["log"] and any(v > 0 for vs in series.values() for v in vs):
                ax.set_yscale("log")
            else:
                ax.set_ylim(0, top * 1.18 if top > 0 else 1)
    else:
        for i, (name, values) in enumerate(spec["series"].items()):
            ax.plot(spec["x"], values, marker=MARKERS[i % len(MARKERS)],
                    color=PALETTE[i % len(PALETTE)], linewidth=1.8,
                    markersize=5, label=name, zorder=3)
            labelled = True
        ax.set_xlabel(spec.get("xlabel", ""), fontsize=9)
        # A doubling sweep on a linear axis crams 1,2,4,8 into the left eighth
        # of the chart and spends half the width on the gap to the last point.
        if _is_geometric(spec["x"]):
            ax.set_xscale("log", base=2)
        # Every x we actually measured, so nobody reads a value off an
        # interpolated tick that was never sampled.
        if spec["x"] and len(spec["x"]) <= 12:
            ax.set_xticks(spec["x"])
            ax.set_xticklabels([f"{x:g}" for x in spec["x"]], fontsize=9)
            ax.minorticks_off()
        top = max((v for vs in spec["series"].values() for v in vs), default=0)
        top = max(top, *spec.get("hlines", {}).values()) if spec.get("hlines") else top
        ax.set_ylim(0, top * 1.15 if top > 0 else 1)

    # A threshold line is a different kind of thing from a measurement, so it
    # gets a different style rather than another colour in the same series.
    rule = ax.axvline if spec.get("horizontal") else ax.axhline
    for name, y in spec.get("hlines", {}).items():
        rule(y, ls="--", color="#a40e26", linewidth=1.4, label=name, zorder=2)
        labelled = True
    if spec.get("hline") is not None:
        rule(spec["hline"], ls="--", color="#a40e26", linewidth=1.4,
             label="budget", zorder=2)
        labelled = True

    if spec.get("horizontal"):
        ax.set_xlabel(spec.get("ylabel", ""), fontsize=9)
    else:
        ax.set_ylabel(spec.get("ylabel", ""), fontsize=9)
    ax.set_title(spec["title"], fontsize=11, fontweight="bold", pad=10)
    ax.grid(True, axis="x" if spec.get("horizontal") else "y",
            ls=":", color="#8b949e", alpha=0.6, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(labelsize=9)
    # One series and no threshold needs no key — the title already says what it
    # is, and a legend box over the data is noise.
    named = len(spec.get("series", {})) + len(spec.get("hlines", {}))
    if labelled and (named > 1 or spec.get("hline") is not None):
        ax.legend(fontsize=8, frameon=True, framealpha=0.95, edgecolor="#d0d7de",
                  loc="best")


def _is_geometric(xs: list[float]) -> bool:
    """Does x roughly double each step? Then it wants a log axis."""
    if len(xs) < 4 or any(x <= 0 for x in xs):
        return False
    ratios = [b / a for a, b in zip(xs, xs[1:])]
    return min(ratios) >= 1.4 and max(ratios) <= 4.0


def _seg_fmt(value: float) -> str:
    """A label that fits inside a stacked segment."""
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 10:
        return f"{value:.0f}"
    return f"{value:.1f}"


def _bar_fmt(values: list[float]) -> str:
    """Enough precision to distinguish the bars, and no more."""
    top = max((abs(v) for v in values), default=0)
    if top >= 1000:
        return "%.0f"
    if top >= 10:
        return "%.1f"
    return "%.3f" if top < 1 else "%.2f"


#: matplotlib by default: the notebook shows exactly what the PDF will contain.
#: Set DODONA_PLOTLY=1 for hover and zoom, at the cost of that guarantee.
USE_PLOTLY = os.environ.get("DODONA_PLOTLY", "") not in ("", "0", "false")


def _show_interactive(spec: dict, note: str = "") -> None:
    """Draw the chart inline. matplotlib unless DODONA_PLOTLY is set."""
    if not USE_PLOTLY:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8.2, 3.8))
        _draw_spec_mpl(ax, spec)
        fig.tight_layout()
        plt.show()
        return
    try:
        import plotly.graph_objects as go
    except ImportError:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8.2, 3.8))
        _draw_spec_mpl(ax, spec)
        fig.tight_layout()
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
    if spec.get("log") and any(v > 0 for vs in spec["series"].values() for v in vs):
        fig.update_yaxes(type="log")
    fig.show()
    if note:
        print(f"  -- {note}")


def _wrap(text: str, width: int) -> list[str]:
    """Greedy wrap. textwrap would do, but this keeps the import list honest."""
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width and line:
            out.append(line); line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out or [""]
