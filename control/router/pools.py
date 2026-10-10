from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

from router.kvbus import KVBus
from gateway.types import KV_SATURATION, Request, SliceOOM, Snapshot, request_messages

PREFILL_CHUNK = 64
DECODE_PACK = 8

def parse_vllm_metrics(text: str) -> dict[str, float]:

    usage: float | None = None
    waiting: float | None = None
    running: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        try:
            value = float(line.rsplit(None, 1)[-1])
        except ValueError:
            continue

        if name in ("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc") and usage is None:
            usage = value
        elif name == "vllm:num_requests_waiting" and waiting is None:
            waiting = value
        elif name == "vllm:num_requests_running" and running is None:
            running = value
    if usage is not None and usage > 1.0:
        usage = usage / 100.0
    out: dict[str, float] = {}
    if usage is not None:
        out["gpu_cache_usage"] = usage
    if waiting is not None:
        out["waiting"] = waiting
    if running is not None:
        out["running"] = running
    return out

class FakeWorker:

    def __init__(
        self,
        id: str,
        *,
        kv_capacity: int = 50_000,
        bus: KVBus | None = None,
    ) -> None:
        self.id = id
        self.kv_capacity = kv_capacity
        self.bus = bus or KVBus()
        self.healthy = True
        self.saturating = False
        self.age_override: float | None = None
        self.soak_weight = 1.0
        self.preempted = False
        self.last_error: str | None = None
        self.decode_tokens_emitted = 0
        self._kv_by_req: dict[str, int] = {}
        self._phase_by_req: dict[str, str] = {}
        self._prefill_left: dict[str, int] = {}
        self._decode_left: dict[str, int] = {}
        self._reqs: dict[str, Request] = {}

    @property
    def kv_used(self) -> int:
        return int(sum(self._kv_by_req.values()))

    def enqueue(self, req: Request, phase: str = "both") -> dict | None:
        if req.aborted:
            return
        cached = self.bus.cached(self.id, req.prefix_hash)
        need = 0
        if phase in ("prefill", "both"):
            need += max(0, int(req.prompt_tokens) - int(cached))
        if phase in ("decode", "both"):
            need += int(req.max_new_tokens)
        if self.kv_used + need > self.kv_capacity:
            self.last_error = "slice_oom"
            raise SliceOOM("slice_oom")
        self._reqs[req.id] = req
        self._phase_by_req[req.id] = phase
        if phase in ("prefill", "both"):
            uncached = max(0, int(req.prompt_tokens) - int(cached))
            self._prefill_left[req.id] = uncached

            self._kv_by_req[req.id] = 0
            self.bus.record(self.id, req.prefix_hash, req.prompt_tokens)
            self._drain_prefill(req.id)
        else:
            self._kv_by_req.setdefault(req.id, 0)
        if phase in ("decode", "both"):
            self._decode_left[req.id] = int(req.max_new_tokens)
        return None

    def step(self) -> None:
        ready = [
            rid
            for rid, left in self._decode_left.items()
            if left > 0 and rid not in self._prefill_left
        ]
        packed = 0
        for rid in ready:
            if packed >= DECODE_PACK:
                break
            take = min(DECODE_PACK - packed, self._decode_left[rid])
            self._decode_left[rid] -= take
            self._kv_by_req[rid] = self._kv_by_req.get(rid, 0) + take
            self.decode_tokens_emitted += take
            packed += take
            if self._decode_left[rid] == 0:
                del self._decode_left[rid]

    def run_until_idle(self) -> None:
        while self._prefill_left or self._decode_left:
            if self._prefill_left:
                for rid in list(self._prefill_left):
                    self._drain_prefill(rid)
            if self._decode_left:
                self.step()

    def abort(self, req_id: str) -> None:
        self._kv_by_req.pop(req_id, None)
        self._prefill_left.pop(req_id, None)
        self._decode_left.pop(req_id, None)
        self._phase_by_req.pop(req_id, None)
        req = self._reqs.pop(req_id, None)
        if req is not None:
            req.aborted = True

    def snapshot(self) -> Snapshot:
        used = self.kv_used
        free = 1.0 - (used / self.kv_capacity) if self.kv_capacity else 0.0
        free = max(0.0, min(1.0, free))
        n = len(self._reqs)
        in_flight = used
        uncached = int(sum(self._prefill_left.values()))
        saturating = self.saturating or (free < (1.0 - KV_SATURATION))
        return Snapshot(
            pod_id=self.id,
            age_s=0.0 if self.age_override is None else float(self.age_override),
            healthy=self.healthy,
            saturating=saturating,
            kv_free_ratio=free,
            tokens_in_flight=in_flight,
            uncached_prefill_tokens=uncached,
            active_requests=n,
            queue_depth=len(self._prefill_left) + len(self._decode_left),
            waiting=len(self._prefill_left),
            running=n,
            prefix_tokens=self.bus.prefixes_for(self.id),
            soak_weight=self.soak_weight,
        )

    def _drain_prefill(self, req_id: str) -> None:
        left = self._prefill_left.get(req_id, 0)
        while left > 0:
            take = min(PREFILL_CHUNK, left)
            self._kv_by_req[req_id] = self._kv_by_req.get(req_id, 0) + take
            left -= take
        self._prefill_left.pop(req_id, None)

class VLLMWorker:

    def __init__(
        self,
        id: str,
        base_url: str,
        *,
        timeout_s: float = 8.0,
        bus: KVBus | None = None,
        model: str = "",
    ) -> None:
        self.id = id
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.bus = bus or KVBus()
        self.model = model or os.environ.get("LOCAL_MODEL", "lab")
        self._last: Snapshot | None = None
        self.healthy = True
        self.saturating = False
        self.kv_capacity = 0

    def enqueue(self, req: Request, phase: str = "both") -> dict | None:
        import json

        payload = {
            "model": self.model,
            "messages": request_messages(req),
            "max_tokens": max(1, int(req.max_new_tokens)),
        }
        raw = self._post("/v1/chat/completions", payload)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"choices": [{"message": {"content": raw[:240]}}]}
        if not isinstance(data, dict):
            return {"choices": [{"message": {"content": str(data)[:240]}}]}
        return data

    def snapshot(self) -> Snapshot:
        try:
            raw = self._get("/metrics", timeout_s=3.0)
        except urllib.error.HTTPError:
            return Snapshot(pod_id=self.id, age_s=0.0, healthy=self.healthy, saturating=self.saturating)
        except (urllib.error.URLError, TimeoutError, OSError):

            return Snapshot(pod_id=self.id, age_s=0.0, healthy=False, saturating=self.saturating)
        parsed = parse_vllm_metrics(raw)
        usage = parsed.get("gpu_cache_usage")
        kv_free = None if usage is None else 1.0 - usage
        waiting = parsed.get("waiting")
        running = parsed.get("running")
        waiting_i = None if waiting is None else int(waiting)
        running_i = None if running is None else int(running)
        active = None
        if waiting_i is not None or running_i is not None:
            active = int((waiting_i or 0) + (running_i or 0))
        snap = Snapshot(
            pod_id=self.id,
            age_s=0.0,
            healthy=True,
            saturating=self.saturating or bool(kv_free is not None and kv_free < (1.0 - KV_SATURATION)),
            kv_free_ratio=kv_free,
            tokens_in_flight=active,
            uncached_prefill_tokens=None,
            active_requests=active,
            queue_depth=waiting_i,
            waiting=waiting_i,
            running=running_i,
            prefix_tokens=None,
            soak_weight=1.0,
        )
        self._last = snap
        return snap

    def abort(self, req_id: str) -> None:
        return None

    def _get(self, path: str, timeout_s: float | None = None) -> str:
        url = self.base_url + path
        with urllib.request.urlopen(url, timeout=timeout_s or self.timeout_s) as resp:
            return resp.read().decode("utf-8")

    def _post(self, path: str, payload: dict[str, Any]) -> str:
        import json

        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            if exc.code >= 500:
                raise
            return raw

def _rewrite_url(url: str) -> str:
    return url.replace("YOUR_LAMBDA_IP", "127.0.0.1")

def _urls(env_name: str) -> list[str]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    return [_rewrite_url(u.strip()) for u in raw.split(",") if u.strip()]

def build_pools(n_fake: int = 2, bus: KVBus | None = None) -> tuple[list[Any], list[Any]]:

    prefill_urls = _urls("PREFILL_URLS")
    if not prefill_urls:
        bus = bus or KVBus()
        prefill = [FakeWorker(f"prefill-{i}", bus=bus) for i in range(n_fake)]
        decode = [FakeWorker(f"decode-{i}", bus=bus) for i in range(n_fake)]
        return prefill, decode

    decode_urls = _urls("DECODE_URLS")
    topology = os.environ.get("LAB_TOPOLOGY", "disaggregated")
    same = (not decode_urls) or decode_urls == prefill_urls or topology == "aggregated"
    bus = bus or KVBus()
    text_model = os.environ.get("TEXT_MODEL", os.environ.get("LOCAL_MODEL", "Qwen/Qwen2.5-3B-Instruct"))
    vision_model = os.environ.get("VISION_MODEL", "Qwen/Qwen2.5-VL-3B-Instruct")
    if same:
        engines = [VLLMWorker(f"engine-{i}", url, bus=bus, model=text_model) for i, url in enumerate(prefill_urls)]
        return engines, engines

    text = [VLLMWorker(f"text-{i}", url, bus=bus, model=text_model) for i, url in enumerate(prefill_urls)]
    vision = [VLLMWorker(f"vision-{i}", url, bus=bus, model=vision_model) for i, url in enumerate(decode_urls)]
    return text, vision
