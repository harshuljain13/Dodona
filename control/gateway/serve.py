from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from edge.guardrails import inspect
from gateway.pipeline import serve_request
from router.overflow import FakeOverflow, OverflowBudget
from router.router import Router, infer_capability
from gateway.admission import Gateway
from gateway.metrics import METRICS, Metrics
from router.pools import build_pools
from gateway.types import request_from_openai

ROOT = Path(__file__).resolve().parents[1]

def prepare_chat(payload: dict) -> tuple[int, dict]:
    edge = inspect(payload)
    if not edge.ok:
        return edge.status, {"error": {"type": edge.reason, "message": edge.message}}
    return 200, edge.payload

def _assistant_text(body: dict) -> str:
    if body.get("error"):
        err = body["error"]
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "error")
        return str(err)
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    first = choices[0]
    msg = first.get("message") if isinstance(first.get("message"), dict) else {}
    text = msg.get("content") or first.get("text") or ""
    return text if isinstance(text, str) else ""

def sse_from_completion(body: dict, *, model: str = "text") -> bytes:
    text = _assistant_text(body)
    chunk = {
        "id": body.get("id") or "orch",
        "object": "chat.completion.chunk",
        "model": body.get("model") or model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")

def listed_models() -> dict:
    ids: list[str] = []
    for env in ("TEXT_MODEL", "VISION_MODEL", "LOCAL_MODEL"):
        value = os.environ.get(env, "").strip()
        if value and value not in ids:
            ids.append(value)
    for alias in ("text", "vision"):
        if alias not in ids:
            ids.append(alias)
    return {"object": "list", "data": [{"id": name, "object": "model", "owned_by": "local-vllm"} for name in ids]}

def evict_prefixes(gateway: Gateway, prefix_hash: str | None = None) -> int:
    workers = list(gateway.router.prefill) + list(gateway.router.decode)
    seen: set[int] = set()
    dropped = 0
    for worker in workers:
        bus = getattr(worker, "bus", None)
        if bus is None or id(bus) in seen:
            continue
        seen.add(id(bus))
        dropped += sum(bus.evict(other.id, prefix_hash) for other in workers)
    return dropped

def build_gateway() -> tuple[Gateway, Metrics]:
    prefill, decode = build_pools()
    return Gateway(Router(prefill, decode)), METRICS

class Handler(BaseHTTPRequestHandler):
    overflow_budget: OverflowBudget
    backup: FakeOverflow | None = None
    metrics: Metrics
    gateway: Gateway

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            self.gateway.refresh_metrics()
            # The gate's own lines, so a scrape distinguishes "the policy admitted
            # everything" from "the policy never loaded". A report that cannot tell those
            # apart is worthless.
            body = (self.metrics.render() + self.gateway.gate.status() + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/v1/models", "/models"):
            body = json.dumps(listed_models()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/v1/chat/completions":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        payload = json.loads(raw or b"{}")
        status, payload = prepare_chat(payload)
        if status != 200:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload.get("kv_evict") and not payload.get("kv_hop"):
            dropped = evict_prefixes(self.gateway, payload.get("prefix_hash"))
            body = json.dumps({"object": "kv.evict", "evicted": dropped}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        req = request_from_openai(payload)
        req.capability = infer_capability(payload) if not req.capability else req.capability
        # The order, in one place: our cluster first — admission, placement,
        # prefill, the KV hop, decode — and a backup endpoint only if the
        # cluster shed for lack of capacity.
        resp = serve_request(
            req, self.gateway, backup=self.backup, budget=self.overflow_budget
        )
        if payload.get("stream") and resp.status == 200:
            body = sse_from_completion(resp.body, model=str(payload.get("model") or "text"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(resp.body).encode("utf-8")
        self.send_response(resp.status)
        self.send_header("Content-Type", "application/json")
        for key, value in resp.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def main(host: str | None = None, port: int | None = None) -> None:
    host = host or os.environ.get("SERVE_HOST", "0.0.0.0")
    port = int(os.environ.get("SERVE_PORT", port or 8080))
    # ROOT is control/; traces live under metrics/ with the other artefacts.
    os.environ.setdefault(
        "TRACE_PATH", str(ROOT.parent / "metrics" / "traces" / "requests.jsonl")
    )
    gw, metrics = build_gateway()
    Handler.gateway = gw
    Handler.metrics = metrics
    # One budget for the process: OVERFLOW_MAX_REQS is a lifetime cap, so it
    # has to outlive the request.
    Handler.overflow_budget = OverflowBudget()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"gateway on http://{host}:{port}", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
