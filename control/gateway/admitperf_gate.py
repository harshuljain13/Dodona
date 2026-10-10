"""Call an AdmitPerf policy from this gateway, and record every decision.

The whole integration, in one file, so the rest of the gateway does not learn about
AdmitPerf. Five lines reach into `gateway/admission.py`; everything else is here.

Configured from the environment:

    ADMISSION_POLICY      kv_threshold | queue_depth | dual_gate | no_admission
    ADMISSION_THRESHOLD   0.90                 a policy parameter
    ADMISSION_MAX_WAITING 60                   a policy parameter
    ADMISSION_ENFORCE     1.0                  fraction of refusals actually applied
    ADMISSION_LOG         traces/single-baseline.jsonl
    ADMISSION_EXPERIMENT  single               what a comparison groups by
    ADMISSION_RUN         baseline             which run within it

Leave ADMISSION_POLICY unset and nothing changes: `Gate.off` is True and the gateway
keeps its own behaviour. An integration that cannot be switched off is one nobody
installs in a request path.

THE BASELINE IS `no_admission`, NOT AN EMPTY POLICY. With the gate off nothing is
logged, so there would be no baseline report to compare against — the comparison would
have one side. `no_admission` admits everything and records each decision with the
metrics behind it, which gives the baseline the same shape of log as any other run.
"""

from __future__ import annotations

import os
from typing import Any

#: A soft dependency on purpose. The gateway pod installs requirements.txt at startup;
#: if admitperf is missing the gateway must still serve, because a logging tool failing
#: to import has no business taking the request path down.
try:
    from admitperf.policies import DualGate, KvThreshold, NoAdmission, QueueDepth

    POLICIES: dict[str, Any] = {
        "kv_threshold": KvThreshold,
        "queue_depth": QueueDepth,
        "dual_gate": DualGate,
        "no_admission": NoAdmission,
    }
    AVAILABLE = True
except ImportError:  # pragma: no cover - only on a box without the package
    POLICIES = {}
    AVAILABLE = False

#: ADMISSION_<NAME> becomes a policy parameter, except these, which configure the
#: recorder. Passing them through would be a TypeError and a gateway that will not start.
RESERVED = frozenset({"POLICY", "ENFORCE", "LOG", "EXPERIMENT", "RUN", "NOTES"})


class Gate:
    """One AdmitPerf policy, asked about the cluster before a request is placed.

    Not a replacement for the gateway's other checks. It runs AFTER the tenant rate
    limit, because a quota refusal is client-attributable (429) and says nothing about
    capacity, and BEFORE `router.place`, because choosing among replicas is a different
    question from whether to serve at all.
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ if env is None else env)
        name = (env.get("ADMISSION_POLICY") or "").strip()

        self.name = name
        self.policy: Any = None
        #: Why the policy is not running, when it is not.
        self.reason: str | None = None
        #: Set when the policy runs but cannot write its log. Decisions still happen and
        #: nothing is recorded, so the report comes back empty for a reason that has
        #: nothing to do with the policy.
        self.log_error: str | None = None

        if not name:
            self.reason = "ADMISSION_POLICY is unset"
            return
        if not AVAILABLE:
            # Named, so /metrics can say the policy did not run and why, rather than
            # leaving a report to claim a policy that was never loaded.
            self.reason = "admitperf is not installed in this image"
            return
        if name not in POLICIES:
            self.reason = f"unknown policy {name!r}; have {sorted(POLICIES)}"
            return

        params: dict[str, Any] = {}
        for key, value in env.items():
            if not key.startswith("ADMISSION_"):
                continue
            short = key[len("ADMISSION_") :]
            if short in RESERVED:
                continue
            try:
                params[short.lower()] = float(value)
            except ValueError:
                params[short.lower()] = value

        kwargs: dict[str, Any] = {
            "experiment": env.get("ADMISSION_EXPERIMENT") or "cluster",
            "run": env.get("ADMISSION_RUN") or "r1",
            "notes": env.get("ADMISSION_NOTES") or None,
            "enforce": float(env.get("ADMISSION_ENFORCE") or 1.0),
            **params,
        }
        log = env.get("ADMISSION_LOG") or None

        try:
            self.policy = POLICIES[name](log=log, **kwargs)
        except OSError as exc:
            # An unwritable log path must NOT disable admission. Constructing the policy
            # opens the log, so a read-only mount once left the gate off entirely —
            # silently switching off the thing under test because a directory was
            # missing. Decide anyway, record nowhere, and say so in /metrics.
            self.log_error = f"{log}: {exc}"
            try:
                self.policy = POLICIES[name](log=None, **kwargs)
            except (TypeError, ValueError) as inner:
                self.reason = f"{name} rejected its parameters: {inner}"
        except (TypeError, ValueError) as exc:
            # A bad parameter is different: the policy cannot run, and substituting a
            # default would measure something nobody asked for.
            self.reason = f"{name} rejected its parameters: {exc}"

    @property
    def off(self) -> bool:
        return self.policy is None

    # -- the metrics the policy reads --------------------------------------

    @staticmethod
    def metrics(router: Any) -> dict[str, float]:
        """Snapshots from the router, under the names AdmitPerf's signals expect.

        vLLM's own names (`vllm:kv_cache_usage_perc`), not this gateway's. A signal is
        only comparable across deployments if every host reports the same quantity under
        the same name, and a number that means something different here than elsewhere
        is worth nothing outside this repo.

        Pressure comes from the replica with the MOST headroom, because the router will
        place there — if even that one is past the threshold, there is nowhere good to
        send the request. An average would let one idle replica hide a saturated fleet.

        A missing value is OMITTED, never zeroed. This is the most dangerous line in the
        file: `kv_cache_usage: 0.0` reads as a completely empty cache, the one value that
        guarantees every policy admits, so a failed scrape would silently switch
        admission off.
        """
        snaps = []
        for pool in ("prefill", "decode"):
            for worker in getattr(router, pool, ()) or ():
                try:
                    snaps.append(worker.snapshot())
                except Exception:  # pragma: no cover - a scrape failure is not fatal
                    continue
        if not snaps:
            return {}

        out: dict[str, float] = {}

        free = [s.kv_free_ratio for s in snaps if s.kv_free_ratio is not None]
        if free:
            out["vllm:kv_cache_usage_perc"] = 1.0 - max(free)

        waiting = [s.waiting for s in snaps if s.waiting is not None]
        if waiting:
            # Summed, not maxed: a queue is work the fleet owes, and it is spread.
            out["vllm:num_requests_waiting"] = float(sum(waiting))

        running = [s.running for s in snaps if s.running is not None]
        if running:
            out["vllm:num_requests_running"] = float(sum(running))

        return out

    # -- the decision -------------------------------------------------------

    def decide(self, router: Any, request_id: str | None = None) -> Any:
        """ADMIT or REJECT, recorded either way. None when the gate is off.

        Never raises. `Policy.__call__` already fails open — a policy that raises admits,
        because failing closed on a bug sheds all traffic — and this extends the same
        guarantee over building the metrics dict.
        """
        if self.policy is None:
            return None
        try:
            return self.policy(self.metrics(router), request_id=request_id)
        except Exception:  # pragma: no cover
            return None

    def outcome(self, request_id: str, **facts: Any) -> None:
        """What happened to a request that was admitted.

        Without it the log holds decisions and no consequences, so a report can say what
        the policy DID but not what it BOUGHT. The join is on request_id.
        """
        if self.policy is None:
            return
        try:
            self.policy.outcome(request_id, **facts)
        except Exception:  # pragma: no cover
            return

    def close(self) -> None:
        """Flush the log. Records are written by a background thread, so a run that ends
        without this can lose its last few decisions — including the ones describing the
        saturation event it was recording."""
        if self.policy is not None:
            try:
                self.policy.log.close()
            except Exception:  # pragma: no cover
                return

    def status(self) -> str:
        """Prometheus lines: whether the policy is live, AND whether it is recording.

        Two separate facts. A live policy with a broken log makes every decision and
        writes none of them down, and the empty report that follows looks exactly like a
        policy that never fired.
        """
        live = 1 if self.policy is not None else 0
        lines = [f'admitperf_gate_active{{policy="{self.name or "none"}"}} {live}']
        if self.policy is not None:
            recording = 0 if (self.log_error or self.policy.log.path is None) else 1
            lines.append(f"admitperf_gate_recording {recording}")
        return "\n".join(lines)
