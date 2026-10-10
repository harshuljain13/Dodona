"""The gateway's AdmitPerf gate — the five lines a host writes.

This is the piece that decides whether a report means anything. Before it existed the
gateway had its own admission logic, so a run could produce a report titled
`kv_threshold` while measuring a tenant rate limit and a hardcoded KV check.

Every assertion here is about a way the gate could be wrong *quietly*: a policy that
never loaded, a metric absent and read as zero, a refusal that takes the request path
down with it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

gate_mod = pytest.importorskip("gateway.admitperf_gate")
Gate = gate_mod.Gate


@dataclass
class Snap:
    """Only the fields the gate reads. None means the scrape did not supply it."""

    kv_free_ratio: float | None = None
    waiting: int | None = None
    running: int | None = None


class Worker:
    def __init__(self, snap: Snap | None, *, raises: bool = False) -> None:
        self._snap = snap
        self._raises = raises

    def snapshot(self) -> Snap:
        if self._raises:
            raise OSError("scrape failed")
        return self._snap  # type: ignore[return-value]


class Router:
    """Stands in for router/router.py. Aggregated mode puts every engine in both lists,
    which is what the real one does."""

    def __init__(self, *snaps: Snap, raises: bool = False) -> None:
        self.prefill = [Worker(s, raises=raises) for s in snaps]
        self.decode: list[Worker] = []


def env(**over: str) -> dict[str, str]:
    base = {
        "ADMISSION_POLICY": "kv_threshold",
        "ADMISSION_THRESHOLD": "0.90",
        "ADMISSION_EXPERIMENT": "test",
        "ADMISSION_RUN": "r1",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Off by default, and off loudly
# --------------------------------------------------------------------------


def test_no_policy_named_means_the_gateway_is_unchanged() -> None:
    """An integration that cannot be turned off is one nobody installs. This also makes
    `policy: ""` the baseline run of the comparison."""
    g = Gate({})
    assert g.off
    assert g.decide(Router(Snap(kv_free_ratio=0.0))) is None
    assert g.status().endswith("0")


def test_an_unknown_policy_name_says_so_rather_than_admitting_silently() -> None:
    """A typo in values would otherwise produce a run that refuses nothing and a report
    naming a policy that never existed."""
    g = Gate(env(ADMISSION_POLICY="kv_thresold"))
    assert g.off
    assert "unknown policy" in (g.reason or "")


def test_a_live_policy_reports_itself_in_metrics(tmp_path: Path) -> None:
    """A scrape must distinguish "the policy admitted everything" from "the policy
    never loaded". Without this line a report cannot tell those apart."""
    g = Gate(env(ADMISSION_LOG=str(tmp_path / "d.jsonl")))
    assert not g.off
    assert 'admitperf_gate_active{policy="kv_threshold"} 1' in g.status()
    assert "admitperf_gate_recording 1" in g.status()


def test_a_policy_that_cannot_write_its_log_still_decides() -> None:
    """A missing log directory must not disable admission. It used to: constructing the
    policy opens the log, the OSError left the gate off, and the thing under test was
    silently turned off because a directory did not exist.

    It decides and reports `recording 0`, so a scrape says the log is the problem rather
    than leaving an empty report to look like a policy that never fired.
    """
    g = Gate(env(ADMISSION_LOG="/definitely/not/writable/d.jsonl"))
    assert not g.off
    d = g.decide(Router(Snap(kv_free_ratio=0.01)), request_id="r1")
    assert d is not None and not d.admitted
    assert "admitperf_gate_recording 0" in g.status()
    assert g.log_error is not None


def test_no_log_path_decides_but_records_nothing() -> None:
    """The chart always sets ADMISSION_LOG. Without it the policy still runs, which is
    the safe direction, and `recording 0` says the report will be empty."""
    g = Gate(env())
    assert not g.off
    assert "admitperf_gate_recording 0" in g.status()


# --------------------------------------------------------------------------
# The metrics it hands the policy
# --------------------------------------------------------------------------


def test_metrics_use_vllms_own_names() -> None:
    """A signal is only comparable across deployments if every host reports the same
    quantity under the same name. Using this gateway's internal names would make the
    number meaningless outside this repo."""
    m = Gate.metrics(Router(Snap(kv_free_ratio=0.3, waiting=5, running=12)))
    assert m["vllm:kv_cache_usage_perc"] == pytest.approx(0.7)
    assert m["vllm:num_requests_waiting"] == 5
    assert m["vllm:num_requests_running"] == 12


def test_a_missing_value_is_omitted_and_never_zeroed() -> None:
    """This is the most dangerous possible bug in the file. A kv_cache_usage of 0.0
    reads as a completely empty cache, which is the one value that guarantees every
    policy admits — so a failed scrape would silently disable admission."""
    m = Gate.metrics(Router(Snap(kv_free_ratio=None, waiting=None, running=None)))
    assert "vllm:kv_cache_usage_perc" not in m
    assert "vllm:num_requests_waiting" not in m
    assert m == {}


def test_pressure_comes_from_the_replica_with_the_most_headroom() -> None:
    """The router places on the best replica, so that is the one whose pressure decides
    whether there is anywhere good to send the request. Averaging would let one idle
    replica hide a saturated fleet."""
    m = Gate.metrics(Router(Snap(kv_free_ratio=0.02), Snap(kv_free_ratio=0.40)))
    # Best replica is 60% used, not the 79% an average would report.
    assert m["vllm:kv_cache_usage_perc"] == pytest.approx(0.60)


def test_queue_depth_is_summed_across_replicas() -> None:
    """A queue is work the fleet owes, and it is spread across replicas. Taking the max
    would understate the backlog."""
    m = Gate.metrics(Router(Snap(waiting=4), Snap(waiting=7)))
    assert m["vllm:num_requests_waiting"] == 11


def test_a_failed_scrape_does_not_take_the_gate_down() -> None:
    """A metrics endpoint timing out must not become a 500 on a request that would
    have been admitted."""
    m = Gate.metrics(Router(Snap(kv_free_ratio=0.5), raises=True))
    assert m == {}


def test_no_workers_yields_no_metrics() -> None:
    assert Gate.metrics(Router()) == {}


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


def test_it_refuses_above_the_threshold(tmp_path: Path) -> None:
    g = Gate(env(ADMISSION_LOG=str(tmp_path / "d.jsonl")))
    d = g.decide(Router(Snap(kv_free_ratio=0.05, waiting=30, running=64)), request_id="r1")
    assert d is not None and not d.admitted
    # 503, not 429: a capacity refusal is the server's fault, and the distinction is
    # what lets a report group refusals by cause across deployments.
    assert d.status == 503
    assert d.reason == "kv_pressure"


def test_it_admits_with_headroom(tmp_path: Path) -> None:
    g = Gate(env(ADMISSION_LOG=str(tmp_path / "d.jsonl")))
    d = g.decide(Router(Snap(kv_free_ratio=0.80, waiting=0, running=3)), request_id="r1")
    assert d is not None and d.admitted


def test_it_admits_when_it_cannot_see_the_signal(tmp_path: Path) -> None:
    """Fail open. A policy that cannot read its signal must not shed all traffic —
    failing closed on a scrape bug takes the whole service down."""
    g = Gate(env(ADMISSION_LOG=str(tmp_path / "d.jsonl")))
    d = g.decide(Router(Snap()), request_id="r1")
    assert d is not None and d.admitted


def test_a_broken_router_does_not_raise_into_the_request_path(tmp_path: Path) -> None:
    g = Gate(env(ADMISSION_LOG=str(tmp_path / "d.jsonl")))

    class Exploding:
        @property
        def prefill(self):  # noqa: ANN202
            raise RuntimeError("boom")

    assert g.decide(Exploding(), request_id="r1") is None


# --------------------------------------------------------------------------
# The log, which is the whole deliverable
# --------------------------------------------------------------------------


def test_every_decision_is_recorded_with_the_metrics_behind_it(tmp_path: Path) -> None:
    """A verdict with no metrics beside it cannot be re-judged later, and `replay`
    exists precisely to judge other policies against the same trace."""
    log = tmp_path / "d.jsonl"
    g = Gate(env(ADMISSION_LOG=str(log)))
    g.decide(Router(Snap(kv_free_ratio=0.05, waiting=9, running=64)), request_id="r1")
    g.decide(Router(Snap(kv_free_ratio=0.90, waiting=0, running=1)), request_id="r2")
    g.policy.log.close()

    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 2
    assert [r["verdict"] for r in rows] == ["reject", "admit"]
    assert rows[0]["metrics"]["vllm:kv_cache_usage_perc"] == pytest.approx(0.95)
    assert rows[0]["experiment"] == "test"
    assert rows[0]["run"] == "r1"
    # Every signal, not just the watched one, so another policy can be replayed.
    assert "queue_depth" in rows[0]["signals"]


def test_an_outcome_joins_to_its_decision_by_request_id(tmp_path: Path) -> None:
    """Decisions without outcomes mean a report can say the policy fired but not
    whether refusing helped. The join is request_id."""
    log = tmp_path / "d.jsonl"
    g = Gate(env(ADMISSION_LOG=str(log)))
    g.decide(Router(Snap(kv_free_ratio=0.80)), request_id="r1")
    g.outcome("r1", latency_ms=1234.0, status=200, served=True)
    g.policy.log.close()

    rows = [json.loads(line) for line in log.read_text().splitlines()]
    ids = [r.get("request_id") for r in rows]
    assert ids.count("r1") == 2
    # Facts are nested under `outcome`, which keeps them from colliding with a
    # decision's own fields when a report reads both kinds of record from one file.
    outcomes = [r["outcome"] for r in rows if "outcome" in r]
    assert outcomes == [{"latency_ms": 1234.0, "status": 200, "served": True}]


def test_outcome_on_a_disabled_gate_is_a_no_op() -> None:
    Gate({}).outcome("r1", latency_ms=1.0)  # must not raise


# --------------------------------------------------------------------------
# Parameters come from the environment the chart fills
# --------------------------------------------------------------------------


def test_policy_parameters_come_from_admission_prefixed_variables() -> None:
    g = Gate(env(ADMISSION_THRESHOLD="0.55"))
    assert g.policy.threshold == pytest.approx(0.55)


def test_reserved_variables_are_not_passed_as_policy_parameters(tmp_path: Path) -> None:
    """ADMISSION_LOG and friends configure the recorder, not the policy. Passing them
    through would be a TypeError at construction and a gateway that will not start."""
    g = Gate(
        env(
            ADMISSION_LOG=str(tmp_path / "d.jsonl"),
            ADMISSION_EXPERIMENT="e",
            ADMISSION_RUN="r",
            ADMISSION_NOTES="n",
            ADMISSION_ENFORCE="0.5",
        )
    )
    assert not g.off
    assert g.policy.enforce == pytest.approx(0.5)


def test_a_different_policy_watches_a_different_signal(tmp_path: Path) -> None:
    """queue_depth exists because below the KV crossover the cache cannot fill, and the
    queue is the only signal that moves."""
    g = Gate(
        env(
            ADMISSION_POLICY="queue_depth",
            ADMISSION_MAX_WAITING="10",
            ADMISSION_LOG=str(tmp_path / "d.jsonl"),
        )
    )
    assert not g.off
    # A full cache but an empty queue: a KV policy would refuse, this one admits.
    d = g.decide(Router(Snap(kv_free_ratio=0.02, waiting=0)), request_id="r1")
    assert d is not None and d.admitted
    d = g.decide(Router(Snap(kv_free_ratio=0.90, waiting=50)), request_id="r2")
    assert d is not None and not d.admitted
