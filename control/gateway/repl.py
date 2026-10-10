from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field

from router.overflow import FakeOverflow, Overflow
from router.router import Router, plan
from gateway.admission import Gateway
from router.kvbus import KVBus
from gateway.metrics import METRICS
from router.pools import FakeWorker
from gateway.types import Request, Response

LEAVE = {503, 529}
STAY = {429, 500, "slice_oom"}

HELP = """
  text [prompt]      GPU A (text model)
  vision [prompt]    GPU B (vision model)
  audio [prompt]     Superlinked (we do not host audio)
  hop [prompt]       prefill → decode KV hop (Mooncake /put)
  evict              drop the last hopped prefix (orch_kv_evict_total)
  kernel [q] [kv]    attention HBM: naive vs flash vs paged
  pressure [need] [policy]  evict-to-fit (lru|lfu|priority|prefix_protect)
  warmup [budget]    replica warmup vs naive; `seed` pins prefixes on kvbus
  board              last hops: which pod, via, TEXT
  metrics            dump gateway.metrics.METRICS (Prometheus text)
  profile            latency by stage (gateway / pick / local / overflow / e2e)
  quit
"""

TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

@dataclass
class Lab:
    split: str = "capability"
    n_a: int = 1
    n_b: int = 1
    kv_a: int = 16_000
    kv_b: int = 16_000
    tokens_per_min: int = 10_000
    seq: int = 0
    history: list[dict] = field(default_factory=list)
    backup: FakeOverflow = field(default_factory=FakeOverflow)
    bus: KVBus = field(default_factory=KVBus)
    left: list[FakeWorker] = field(default_factory=list)
    right: list[FakeWorker] = field(default_factory=list)
    gw: Gateway | None = None
    overflow: Overflow | None = None
    force_fake: bool = False

    def __post_init__(self) -> None:
        self.rebuild()

    def live(self) -> bool:
        return (not self.force_fake) and bool(os.environ.get("PREFILL_URLS", "").strip())

    def names(self) -> tuple[str, str]:
        return ("text", "vision") if self.split == "capability" else ("prefill", "decode")

    def rebuild(self) -> None:
        os.environ["LAB_SPLIT"] = self.split
        overflow = bool(os.environ.get("OVERFLOW_BASE_URL", "").strip())
        if self.live():
            from router.pools import build_pools

            self.left, self.right = build_pools()
            self.n_a, self.n_b = len(self.left), len(self.right)
            self.gw = Gateway(Router(self.left, self.right), tokens_per_min=self.tokens_per_min)
            self.backup = FakeOverflow()
            self.overflow = Overflow(local=self.gw, backup=None if overflow else self.backup)
            METRICS.reset()
            return
        a, b = self.names()
        self.bus = KVBus()
        self.left = [FakeWorker(f"{a}-{i}", kv_capacity=self.kv_a, bus=self.bus) for i in range(self.n_a)]
        self.right = [FakeWorker(f"{b}-{i}", kv_capacity=self.kv_b, bus=self.bus) for i in range(self.n_b)]
        self.gw = Gateway(Router(self.left, self.right), tokens_per_min=self.tokens_per_min)
        self.backup = FakeOverflow()
        self.overflow = Overflow(local=self.gw, backup=None if overflow and not self.force_fake else self.backup)
        METRICS.reset()

    def exec(self, line: str) -> list[str]:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            return []
        parts = raw.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("q", "quit", "exit"):
            return ["quit"]
        fn = {
            "help": lambda _: [HELP.strip()],
            "?": lambda _: [HELP.strip()],
            "status": lambda _: self._status(),
            "pods": lambda _: self._status(),
            "board": lambda _: self._board(),
            "codes": lambda _: self._codes(),
            "replicas": self._replicas,
            "slice": self._slice,
            "split": self._split,
            "cap": self._cap,
            "send": self._send,
            "text": lambda a: self._send(["text", *a]),
            "vision": lambda a: self._send(["vision", *a]),
            "audio": lambda a: self._send(["audio", *a]),
            "hop": self._hop,
            "evict": self._evict,
            "kernel": self._kernel,
            "pressure": self._pressure,
            "warmup": self._warmup,
            "flood": self._flood,
            "saturate": self._saturate,
            "healthy": self._healthy,
            "force": self._force,
            "plan": lambda _: self._plan(),
            "metrics": lambda _: self._metrics(),
            "profile": lambda _: self._profile(),
            "reset": self._reset,
        }.get(cmd)
        if fn is None:
            return [f"unknown: {cmd}  (help)"]
        return fn(args)

    def _status(self) -> list[str]:
        a, b = self.names()
        mode = "LIVE vLLM" if self.live() else ("LIVE overflow-only" if os.environ.get("OVERFLOW_BASE_URL") and not self.force_fake else "FAKE")
        overflow = os.environ.get("OVERFLOW_BACKEND", "") or ("set" if os.environ.get("OVERFLOW_BASE_URL") else "off")
        leave = "leave 503, 529     stay 429, 500, slice_oom"
        lines = [
            f"{mode}  split={self.split}  {a}×{len(self.left)}  {b}×{len(self.right)}  overflow={overflow}",
            leave,
            "",
        ]
        for pool, workers in ((a, self.left), (b, self.right)):
            phase = "both" if self.split == "capability" else pool
            if not workers:
                lines.append(f"{'—':<14} {pool:<8} {phase:<8}  no replicas")
            for w in workers:
                url = getattr(w, "base_url", "")
                model = getattr(w, "model", "")
                if url:
                    lines.append(f"{w.id:<14} {pool:<8} {phase:<8}  {model}  {url}")
                    continue
                snap = w.snapshot()
                state = "saturating" if getattr(w, "saturating", False) or snap.saturating else "ready"
                cap = getattr(w, "kv_capacity", 0)
                lines.append(
                    f"{w.id:<14} {pool:<8} {phase:<8}  kv {snap.tokens_in_flight or 0}/{cap}  {state}"
                )
        lines.append("")
        lines.append("Type  text …   or  vision …   or  audio …     then wait for TEXT.")
        return lines

    def _board(self) -> list[str]:
        if not self.history:
            return ["board empty — send or force first"]
        lines = [f"{'id':<14} {'cap':<7} {'pod / phase':<28} {'via':<9} {'code':<12} why"]
        for ev in self.history[-12:]:
            lines.append(
                f"{ev['id']:<14} {ev['cap']:<7} {ev['pods']:<28} {ev['via']:<9} {ev['code']:<12} {ev['why']}"
            )
        return lines

    def _codes(self) -> list[str]:
        return [
            "local code     Overflow",
            "200            stay   served on the assigned pod",
            "429            stay   ADMIT tenant cap — never overflow a 429",
            "500            stay   one local retry, then fail",
            "slice_oom      stay   replica gpumem exceeded — refuse, no pool hop",
            "503            LEAVE  fleet saturating / no eligible pod → backup",
            "529            LEAVE  same as 503 (overloaded)",
            "other          stay",
            "",
            "Lambda backup is Superlinked. This playground uses FakeOverflow (no spend).",
            "force 503   /   saturate text then send text   /   replicas vision 0 then send vision",
        ]

    def _replicas(self, args: list[str]) -> list[str]:
        if self.live():
            return [
                "live fleet is PREFILL_URLS / DECODE_URLS. "
                "On Lambda: kubectl scale deploy/vllm-prefill --replicas=N. "
                "Or python -m gateway.repl --fake to spawn FakeWorkers."
            ]
        a, b = self.names()
        aliases = {"text": "a", "prefill": "a", "vision": "b", "decode": "b"}
        if not args:
            return [f"{a}={self.n_a} {b}={self.n_b}"]
        if args[0].isdigit() and (len(args) == 1 or args[1].isdigit()):
            self.n_a = int(args[0])
            if len(args) > 1:
                self.n_b = int(args[1])
        else:
            i = 0
            while i < len(args):
                key = aliases.get(args[i])
                if key is None or i + 1 >= len(args) or not args[i + 1].lstrip("-").isdigit():
                    return [f"usage: replicas {a} N [{b} M]"]
                n = int(args[i + 1])
                if n < 0:
                    return ["replicas must be >= 0"]
                if key == "a":
                    self.n_a = n
                else:
                    self.n_b = n
                i += 2
        self.rebuild()
        return [f"replicas {a}={self.n_a} {b}={self.n_b}"] + self._status()[:4]

    def _slice(self, args: list[str]) -> list[str]:
        if self.live():
            return ["slice size is HAMi gpumem on Lambda (k8s-config/hami/hami-lambda.yaml). --fake to play with KV caps."]
        a, b = self.names()
        if not args:
            return [f"slice {a}={self.kv_a} {b}={self.kv_b}  (KV tokens ≈ HAMi gpumem MiB on Lambda)"]
        aliases = {"text": "a", "prefill": "a", "vision": "b", "decode": "b"}
        if args[0].isdigit():
            n = int(args[0])
            if n <= 0:
                return ["slice must be > 0"]
            self.kv_a = self.kv_b = n
        elif args[0] in aliases and len(args) >= 2 and args[1].isdigit():
            n = int(args[1])
            if n <= 0:
                return ["slice must be > 0"]
            if aliases[args[0]] == "a":
                self.kv_a = n
            else:
                self.kv_b = n
        else:
            return [f"usage: slice N  |  slice {a}|{b} N"]
        for w in self.left:
            w.kv_capacity = self.kv_a
        for w in self.right:
            w.kv_capacity = self.kv_b
        return [f"slice {a}={self.kv_a} {b}={self.kv_b}"]

    def _split(self, args: list[str]) -> list[str]:
        if not args or args[0] not in ("capability", "phase"):
            return ["usage: split capability|phase"]
        self.split = args[0]
        self.rebuild()
        return [f"split={self.split}"] + self._status()

    def _cap(self, args: list[str]) -> list[str]:
        if not args or not args[0].isdigit():
            return [f"cap={self.tokens_per_min}"]
        self.tokens_per_min = int(args[0])
        if self.gw is not None:
            self.gw.tokens_per_min = self.tokens_per_min
        return [f"tenant cap {self.tokens_per_min} tokens/min"]

    def _send(self, args: list[str]) -> list[str]:
        if not args:
            return ["usage: send text|vision|audio [tokens=N] [your prompt...]"]
        cap = args[0].lower()
        tokens = 64
        words: list[str] = []
        for item in args[1:]:
            if item.startswith("tokens="):
                tokens = int(item.split("=", 1)[1])
            else:
                words.append(item)
        prompt = " ".join(words)
        if not prompt:
            prompt = {
                "text": "Write one sentence about a GPU.",
                "vision": "What do you see? One sentence.",
                "audio": "Transcribe this: hello from class 10.",
            }.get(cap, "hello")
        messages = None
        if cap == "vision":
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": TINY_PNG}},
                    ],
                }
            ]
        return self._dispatch(cap, tokens, prompt=prompt, messages=messages)

    def _flood(self, args: list[str]) -> list[str]:
        n = int(args[0]) if args and args[0].isdigit() else 8
        start = len(self.history)
        for _ in range(n):
            req = self._req("text", "text", 64)
            assert self.overflow is not None
            resp = self.overflow.send(req)
            pods, why = self._explain(req, resp)
            self._record(req, resp, pods=pods, why=why)
        chunk = self.history[start:]
        picked = [ev["pods"] for ev in chunk]
        sheds = [ev for ev in chunk if ev["via"] != "local" or ev["code"] not in ("200",)]
        return [
            f"flood {n}  pods: {picked}",
            f"overflow_or_shed: {len(sheds)}  via_overflow={sum(1 for ev in chunk if ev['via'] == 'overflow')}",
        ]

    def _saturate(self, args: list[str]) -> list[str]:
        targets = self._select(args[0] if args else "all")
        if isinstance(targets, str):
            return [targets]
        for w in targets:
            w.saturating = True
        return [f"saturate {[w.id for w in targets]} — next PLACE on that pool sheds 503"]

    def _healthy(self, args: list[str]) -> list[str]:
        targets = self._select(args[0] if args else "all")
        if isinstance(targets, str):
            return [targets]
        for w in targets:
            w.saturating = False
            w.healthy = True
        return [f"healthy {[w.id for w in targets]}"]

    def _force(self, args: list[str]) -> list[str]:
        if not args:
            return ["usage: force 429|500|503|529|slice_oom"]
        token = args[0].lower()
        if token == "slice_oom":
            local = _Forced(503, error="slice_oom")
            label = "slice_oom"
        elif token.isdigit() and int(token) in (429, 500, 503, 529):
            code = int(token)
            err = "rate_limit_error" if code == 429 else ("server_is_overloaded" if code in LEAVE else "internal")
            local = _Forced(code, error=err)
            label = str(code)
        else:
            return ["usage: force 429|500|503|529|slice_oom"]
        req = self._req("force", "text", 8)
        resp = Overflow(local=local, backup=self.backup).send(req)
        why = f"injected local {label} → Overflow {'LEAVE' if resp.via == 'overflow' else 'stay'}"
        self._record(req, resp, pods="(forced local)", why=why)
        return self._format(req, resp, pods="(forced local)", why=why)

    def _metrics(self) -> list[str]:
        if self.gw is not None:
            self.gw.refresh_metrics()
        text = METRICS.render().rstrip()
        return text.splitlines() or ["(empty metrics)"]

    def _profile(self) -> list[str]:
        return METRICS.profile_summary()

    def _plan(self) -> list[str]:
        uncached = sum(int(w.snapshot().uncached_prefill_tokens or 0) for w in self.left)
        budget = sum(w.kv_capacity for w in self.left) or 1
        slots_used = sum(int(w.snapshot().active_requests or 0) for w in self.right)
        slots = max(1, len(self.right) * 8)
        sink = io.StringIO()
        with redirect_stdout(sink):
            wanted = plan(
                current_prefill=max(1, self.n_a),
                current_decode=max(1, self.n_b),
                uncached_prefill_tokens_in_flight=uncached,
                prefill_token_budget=budget,
                decode_slots_used=slots_used,
                decode_slots=slots,
            )
        return [
            sink.getvalue().rstrip(),
            f"planner print only: desired {wanted}  (KEDA actuates this on Lambda, not here)",
        ]

    def _reset(self, _args: list[str]) -> list[str]:
        self.split = "capability"
        self.n_a = 1
        self.n_b = 1
        self.kv_a = 16_000
        self.kv_b = 16_000
        self.tokens_per_min = 10_000
        self.history.clear()
        self.rebuild()
        return ["reset"] + self._status()[:3]

    def _orch_base(self) -> str:
        raw = os.environ.get("ORCH_URL", "").strip()
        if raw:
            return raw.rstrip("/")
        if self.live():
            return "http://127.0.0.1:8080/v1"
        return ""

    def _orch_kv(self, *, hop: bool, evict: bool, prompt: str) -> list[str]:
        import json
        import urllib.error
        import urllib.request

        base = self._orch_base()
        payload = {
            "model": "text",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "tenant": "lab-kv",
            "kv_hop": hop,
            "kv_evict": evict,
        }
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8") or "{}")
            status = exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return [f"orch unreachable ({exc}). Is Step 2 ssh.sh still up?"]
        if evict and not hop:
            return [f"evict orch status={status} evicted={body.get('evicted')}"]
        return [
            f"hop orch status={status}",
            f"  HANDOFF  kv_hop={body.get('kv_hop')}",
            f"  TEXT     {_completion_text(body)}",
        ]

    def _hop(self, args: list[str]) -> list[str]:
        prompt = " ".join(args) or "Write one sentence about a GPU."
        if self._orch_base():
            return self._orch_kv(hop=True, evict=False, prompt=prompt)
        req = self._req("hop", "text", 32, prompt=prompt)
        req.force_phase = True
        assert self.overflow is not None
        resp = self.overflow.send(req)
        pods, why = self._explain(req, resp)
        self._record(req, resp, pods=pods, why=why)
        return self._format(req, resp, pods=pods, why=why)

    def _evict(self, _args: list[str]) -> list[str]:
        if self._orch_base():
            return self._orch_kv(hop=False, evict=True, prompt="evict")
        n = 0
        if self.bus.hops:
            last = self.bus.hops[-1]
            n += self.bus.evict(str(last.get("src") or ""), last.get("prefix"))
            n += self.bus.evict(str(last.get("dst") or ""), last.get("prefix"))
        else:
            for worker in self.left + self.right:
                n += self.bus.evict(worker.id)
        return [f"evict dropped {n} prefix(es)  orch_kv_evict_total={METRICS.kv_evict_total}"]

    def _kernel(self, args: list[str]) -> list[str]:
        from kernels.engineering import KernelShape, report

        q_len = 128
        kv_len = 2048
        if args and args[0].isdigit():
            q_len = int(args[0])
        if len(args) > 1 and args[1].isdigit():
            kv_len = int(args[1])
        return report(KernelShape(q_len=q_len, kv_len=kv_len))

    def _pressure(self, args: list[str]) -> list[str]:
        from router.kv_eviction import apply, demo, from_bus, report

        need = 1024
        policy = "prefix_protect"
        for arg in args:
            if arg.isdigit():
                need = int(arg)
            else:
                policy = arg
        if self.bus._cached:
            cap = min(
                (int(getattr(w, "kv_capacity", 16_000)) for w in self.left + self.right),
                default=16_000,
            )
            cache = from_bus(self.bus, capacity_tokens=cap)
            victims = cache.evict(need, policy=policy)
            dropped = apply(self.bus, victims)
            lines = report(cache, victims, policy)
            lines.append(f"applied {dropped}  orch_kv_evict_total={METRICS.kv_evict_total}")
            return lines
        return demo(policy, need)

    def _warmup(self, args: list[str]) -> list[str]:
        from router.warmup import apply, choose_plan, naive_plan, report

        if args and args[0] == "seed":
            prefixes = {"sys": 256, "tool": 64}
            workers = [w.id for w in self.left + self.right]
            n = apply(self.bus, prefixes, workers)
            return [f"warmup seed prefixes={n} workers={len(workers)} sys=256 tool=64"]
        budget = 1500
        naive = False
        for arg in args:
            if arg.isdigit():
                budget = int(arg)
            elif arg == "naive":
                naive = True
        plan = naive_plan() if naive else choose_plan(budget_ms=budget)
        return report(plan)

    def _dispatch(
        self,
        cap: str,
        tokens: int,
        prompt: str = "",
        messages: list | None = None,
    ) -> list[str]:
        req = self._req(cap, cap, tokens, prompt=prompt, messages=messages)
        assert self.overflow is not None
        resp = self.overflow.send(req)
        pods, why = self._explain(req, resp)
        self._record(req, resp, pods=pods, why=why)
        return self._format(req, resp, pods=pods, why=why)

    def _req(
        self,
        name: str,
        cap: str,
        tokens: int,
        prompt: str = "",
        messages: list | None = None,
    ) -> Request:
        self.seq += 1
        return Request(
            id=f"{name}-{self.seq}",
            arrival_t=float(self.seq),
            priority=1,
            prompt_tokens=max(16, tokens),
            max_new_tokens=tokens if tokens > 8 else (64 if self.live() else 8),
            prefix_hash=f"lab-{self.seq}",
            timeout_s=20.0 if self.live() else 30.0,
            tenant="lab",
            capability=cap,
            prompt=prompt,
            messages=messages,
        )

    def _explain(self, req: Request, resp: Response) -> tuple[str, str]:
        h = resp.handoff
        a, b = self.names()
        if resp.status == 429:
            return "—", "ADMIT 429 tenant — stay"
        if resp.error == "slice_oom":
            return "—", "PLACE slice_oom — stay (too big for this replica size)"
        if h is None:
            if req.capability not in ("text", "vision"):
                return "—", f"BIND no pool for {req.capability} — 503, Overflow may leave"
            pool = a if req.capability == "text" or self.split == "phase" else b
            if req.capability == "vision" and self.split == "capability":
                pool = b
            return "—", f"PLACE no eligible {pool} pod ({resp.reason}) — 503, may leave"
        if self.split == "capability" and h.prefill is h.decode:
            pool = a if req.capability == "text" else b
            return (
                f"{h.prefill.id} both",
                f"BIND {req.capability} → {pool}  PLACE {h.prefill.id}  phase=both (no KV hop)",
            )
        hop = "kv_hop" if h.prefill is not h.decode else "same-pod"
        return (
            f"prefill={h.prefill.id} decode={h.decode.id}",
            f"phase PLACE prefill={h.prefill.id} decode={h.decode.id} ({hop})",
        )

    def _format(self, req: Request, resp: Response, *, pods: str, why: str) -> list[str]:
        hop = resp.body.get("kv_hop")
        text = _completion_text(resp.body)
        model = resp.body.get("model") or ""
        lines = [
            f"== {req.id} capability={req.capability} ==",
            f"  ADMIT    {'429 tenant' if resp.status == 429 else 'ok'}",
            f"  BIND     {req.capability}  split={self.split}",
            f"  PLACE    {pods}",
            f"  HANDOFF  kv_hop={hop}",
            f"  ROUTE    via={resp.via}  status={resp.status}  local_status={resp.local_status}  error={resp.error}",
            f"  WHY      {why}",
        ]
        if model:
            lines.append(f"  MODEL    {model}")
        if text:
            lines.append(f"  TEXT     {text}")
        elif resp.status == 200 and resp.via == "local" and not self.live():
            lines.append("  TEXT     (FakeWorker — no tokens. source .env and restart without --fake)")
        return lines

    def _record(self, req: Request, resp: Response, *, pods: str, why: str) -> None:
        code = "slice_oom" if resp.error == "slice_oom" else str(resp.local_status or resp.status)
        self.history.append(
            {
                "id": req.id,
                "cap": req.capability,
                "pods": pods,
                "via": resp.via,
                "code": code,
                "why": why,
            }
        )

    def _select(self, token: str) -> list | str:
        a, b = self.names()
        workers = self.left + self.right
        if token == "all":
            return workers
        if token in (a, "left"):
            return list(self.left)
        if token in (b, "right"):
            return list(self.right)
        for w in workers:
            if w.id == token:
                return [w]
        return f"unknown pod {token}  (pods to list ids)"

def _completion_text(body: dict) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = msg.get("content") or first.get("text") or ""
        if isinstance(text, str) and text.strip():
            return text.strip()
    raw = body.get("text")
    return raw.strip() if isinstance(raw, str) else ""

class _Forced:
    def __init__(self, status: int, error: str | None = None) -> None:
        self.status = status
        self.error = error

    def handle(self, req: Request) -> Response:
        reason = "tenant_tokens" if self.status == 429 else "no_eligible_pod"
        return Response(status=self.status, error=self.error, reason=reason)

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    lab = Lab(force_fake="--fake" in argv)
    interactive = sys.stdin.isatty() and "--script" not in argv
    if interactive:
        print("Gateway. text → GPU A.  vision → GPU B.  audio → Superlinked.")
        print("One line. Wait for TEXT.")
        print("\n".join(lab._status()))
        print()
        while True:
            try:
                raw = input("lab> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            out = lab.exec(raw)
            if out == ["quit"]:
                return 0
            if out:
                print("\n".join(out))
                print()
        return 0
    for raw in sys.stdin:
        out = lab.exec(raw)
        if out == ["quit"]:
            return 0
        if out:
            print("\n".join(out))
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(0)
