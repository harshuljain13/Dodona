"""Bursting to a backup endpoint when our own cluster has no room.

This module does ONE thing: call someone else's OpenAI-compatible endpoint. It
knows nothing about admission, placement, the KV hop or tracing — see
gateway/pipeline.py for the order those happen in, and who calls this.

It deliberately imports nothing from the admission, queue, routing or pool
modules. A relief valve that can reach into the control plane stops being a
relief valve, so tests/test_overflow.py greps this file to keep it that way.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

from gateway.types import Request, Response, request_messages

#: Default ceiling on tokens we will pay someone else to generate.
DEFAULT_MAX_TOKENS = 64


class OverflowPort(Protocol):
    """A backup that can answer a request. Implemented for real by HTTP."""

    calls: list[Request]

    def complete(self, req: Request) -> Response: ...


class FakeOverflow:
    """An in-process backup, so the suite never makes a paid call."""

    def __init__(self) -> None:
        self.calls: list[Request] = []

    def complete(self, req: Request) -> Response:
        self.calls.append(req)
        return Response(status=200, body={"id": "overflow-fake", "choices": []})


class OverflowBudget:
    """A LIFETIME cap on how many requests may leave, not a rate.

    OVERFLOW_MAX_REQS defaults to unlimited. Once spent, the path switches
    itself off — which in a report looks exactly like "overflow did not help"
    when what happened is that overflow stopped being available. `spent` is
    public so the run summary can say which it was.
    """

    def __init__(self, limit: int | None = None) -> None:
        if limit is None:
            raw = os.environ.get("OVERFLOW_MAX_REQS", "").strip()
            try:
                limit = int(raw) if raw else -1
            except ValueError:
                limit = 0
        self.limit = limit
        self.spent = 0

    def allows(self) -> bool:
        return self.limit < 0 or self.spent < self.limit

    def charge(self) -> None:
        self.spent += 1


def is_configured() -> bool:
    """Is there anywhere to burst to at all."""
    return bool(os.environ.get("OVERFLOW_BASE_URL", "").strip())


def call_backup(req: Request) -> Response:
    """Send one request to the configured backup endpoint."""
    base = os.environ.get("OVERFLOW_BASE_URL", "").rstrip("/")
    model = os.environ.get("OVERFLOW_MODEL", "")
    key = os.environ.get("OVERFLOW_API_KEY", "")
    try:
        return chat_completions(
            base, req, model=model, api_key=key, max_tokens=_capped_tokens(req)
        )
    except Exception as exc:
        return Response(
            status=502,
            error="overloaded_error",
            body={"error": {"type": "overloaded_error", "message": str(exc)}},
        )


def backend_name() -> str | None:
    return os.environ.get("OVERFLOW_BACKEND", "").strip() or None


def _capped_tokens(req: Request) -> int:
    raw = os.environ.get("OVERFLOW_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))
    try:
        cap = int(raw)
    except ValueError:
        cap = DEFAULT_MAX_TOKENS
    return max(1, min(int(req.max_new_tokens), cap))


def chat_completions(
    base: str,
    req: Request,
    *,
    model: str,
    api_key: str,
    max_tokens: int,
) -> Response:
    """POST /chat/completions, via the openai client if it is installed."""
    try:
        from openai import OpenAI

        client = OpenAI(base_url=base, api_key=api_key or "not-needed", timeout=req.timeout_s)
        out = client.chat.completions.create(
            model=model or "lab",
            messages=request_messages(req),
            max_tokens=max_tokens,
        )
        return Response(
            status=200,
            body=out.model_dump() if hasattr(out, "model_dump") else {"id": getattr(out, "id", "")},
        )
    except ImportError:
        pass

    payload = {
        "model": model or "lab",
        "messages": request_messages(req),
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    http_req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
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
