from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from gateway.metrics import METRICS
from router.trace import TRACES
from gateway.types import Request, Response, request_messages

class OverflowPort(Protocol):

    calls: list[Request]

    def complete(self, req: Request) -> Response: ...

class FakeOverflow:

    def __init__(self) -> None:
        self.calls: list[Request] = []

    def complete(self, req: Request) -> Response:
        self.calls.append(req)
        return Response(status=200, body={"id": "overflow-fake", "choices": []})

class Overflow:

    def __init__(
        self,
        local: Any | None = None,
        backup: OverflowPort | None = None,
    ) -> None:
        self.local = local
        self.backup = backup
        self._overflow_used = 0

    def send(self, req: Request) -> Response:
        t0 = time.perf_counter()
        resp = self._call_local(req)
        local_ms = _ms(t0)
        if resp.status == 200:
            return self._finish(req, resp, via="local", local_ms=local_ms)
        if resp.status == 429:
            return self._finish(req, resp, via="local", local_ms=local_ms)
        if resp.status == 500:
            t1 = time.perf_counter()
            retry = self._call_local(req)
            return self._finish(req, retry, via="local", local_ms=local_ms + _ms(t1))
        if resp.error == "slice_oom":
            return self._finish(req, resp, via="local", local_ms=local_ms)
        if resp.status in (503, 529):
            return self._overflow(req, resp, local_ms=local_ms)
        return self._finish(req, resp, via="local", local_ms=local_ms)

    def _call_local(self, req: Request) -> Response:
        if self.local is not None:
            return self.local.handle(req)
        base = os.environ.get("LOCAL_BASE_URL", "").rstrip("/")
        if not base:
            return Response(status=503, error="server_is_overloaded")
        return _chat_completions(
            base,
            req,
            model=os.environ.get("LOCAL_MODEL", ""),
            api_key="",
            max_tokens=int(req.max_new_tokens),
        )

    def _overflow(self, req: Request, local_resp: Response, *, local_ms: float) -> Response:
        cap = os.environ.get("OVERFLOW_MAX_REQS", "").strip()
        if cap:
            try:
                limit = int(cap)
            except ValueError:
                limit = 0
            if limit >= 0 and self._overflow_used >= limit:
                return self._finish(req, local_resp, via="local", local_ms=local_ms)
        if self.backup is not None:
            self._overflow_used += 1
            METRICS.inc_overflow()
            t1 = time.perf_counter()
            out = self.backup.complete(req)
            return self._finish(
                req,
                out,
                via="overflow",
                local_status=local_resp.status,
                local_reason=local_resp.reason,
                local_ms=local_ms,
                overflow_ms=_ms(t1),
            )
        if not os.environ.get("OVERFLOW_BASE_URL", "").strip():
            return self._finish(req, local_resp, via="local", local_ms=local_ms)
        self._overflow_used += 1
        METRICS.inc_overflow()
        t1 = time.perf_counter()
        out = _backup_http(req)
        return self._finish(
            req,
            out,
            via="overflow",
            local_status=local_resp.status,
            local_reason=local_resp.reason,
            local_ms=local_ms,
            overflow_ms=_ms(t1),
            backend=os.environ.get("OVERFLOW_BACKEND", "").strip() or None,
        )

    def _finish(
        self,
        req: Request,
        resp: Response,
        *,
        via: str,
        local_status: int | None = None,
        local_reason: str | None = None,
        backend: str | None = None,
        local_ms: float = 0.0,
        overflow_ms: float | None = None,
    ) -> Response:
        resp.via = via
        resp.local_status = local_status
        resp.headers["X-Via"] = via
        resp.body["via"] = via
        if local_status is not None:
            resp.body["local_status"] = local_status
        total_ms = local_ms + (overflow_ms or 0.0)
        METRICS.observe_duration("local", local_ms / 1000.0)
        METRICS.observe_duration("e2e", total_ms / 1000.0)
        if overflow_ms is not None:
            METRICS.observe_duration("overflow", overflow_ms / 1000.0)
        event = {
            "id": req.id,
            "tenant": req.tenant,
            "ts": time.time(),
            "via": via,
            "happened": _happened(via, resp, local_status),
            "status": resp.status,
            "error": resp.error,
            "reason": local_reason or resp.reason,
            "local_ms": round(local_ms, 3),
            "total_ms": round(total_ms, 3),
        }
        if local_status is not None:
            event["local_status"] = local_status
        if overflow_ms is not None:
            event["overflow_ms"] = round(overflow_ms, 3)
        if backend:
            event["backend"] = backend
        event.update(_outcome(resp.body))
        if isinstance(resp.body.get("kv_hop"), dict):
            event["kv_hop"] = resp.body["kv_hop"]
        TRACES.record(event)
        return resp

def send(req: Request, local: Any, backup: OverflowPort | None = None) -> Response:
    return Overflow(local=local, backup=backup).send(req)

def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0

def _happened(via: str, resp: Response, local_status: int | None) -> str:
    if via == "overflow":
        return "overflow_ok" if resp.status == 200 else "overflow_error"
    if resp.status == 200:
        return "served_local"
    if resp.status == 429:
        return "tenant_refuse"
    if resp.error == "slice_oom":
        return "refuse"
    if (local_status or resp.status) in (503, 529):
        return "shed_no_leave"
    return "local_error"

def _outcome(body: dict) -> dict:
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    text = ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = msg.get("content") or first.get("text") or ""
    out: dict = {}
    if body.get("model"):
        out["model"] = body["model"]
    if body.get("id") and body["id"] != "overflow-fake":
        out["completion_id"] = body["id"]
    if usage.get("prompt_tokens") is not None:
        out["prompt_tokens"] = usage["prompt_tokens"]
    if usage.get("completion_tokens") is not None:
        out["completion_tokens"] = usage["completion_tokens"]
    if usage.get("total_tokens") is not None:
        out["total_tokens"] = usage["total_tokens"]
    if isinstance(text, str) and text:
        out["text"] = text[:160]
    return out

def _clamp_max_tokens(req: Request) -> int:
    raw = os.environ.get("OVERFLOW_MAX_TOKENS", "64")
    try:
        cap = int(raw)
    except ValueError:
        cap = 64
    return max(1, min(int(req.max_new_tokens), cap))

def _backup_http(req: Request) -> Response:
    base = os.environ.get("OVERFLOW_BASE_URL", "").rstrip("/")
    model = os.environ.get("OVERFLOW_MODEL", "")
    key = os.environ.get("OVERFLOW_API_KEY", "")
    try:
        return _chat_completions(base, req, model=model, api_key=key, max_tokens=_clamp_max_tokens(req))
    except Exception as exc:
        return Response(status=502, error="overloaded_error", body={"error": {"type": "overloaded_error", "message": str(exc)}})

def _chat_completions(
    base: str,
    req: Request,
    *,
    model: str,
    api_key: str,
    max_tokens: int,
) -> Response:

    try:
        from openai import OpenAI

        client = OpenAI(base_url=base, api_key=api_key or "not-needed", timeout=req.timeout_s)
        out = client.chat.completions.create(
            model=model or "lab",
            messages=request_messages(req),
            max_tokens=max_tokens,
        )
        return Response(status=200, body=out.model_dump() if hasattr(out, "model_dump") else {"id": getattr(out, "id", "")})
    except ImportError:
        pass

    payload = {
        "model": model or "lab",
        "messages": request_messages(req),
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    http_req = urllib.request.Request(
        f"{base}/chat/completions",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_req, timeout=req.timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return Response(status=200, body=body)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"error": {"message": raw}}
        return Response(status=int(exc.code), body=body)
