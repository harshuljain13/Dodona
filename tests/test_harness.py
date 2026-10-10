from __future__ import annotations

from gateway.harness import (
    BEHAVIORS,
    behavior_fleet_soak,
    behavior_ignore_stale_pod,
    behavior_overflow_on_503,
    behavior_tenant_429_stays,
    run_behavior,
)

def test_behavior_fleet_soak_sheds_and_completes() -> None:
    r = behavior_fleet_soak()
    assert r.passed, r.summary
    assert r.extra["completed"] > 0
    assert r.extra["shed_503"] > 0

def test_behavior_tenant_429_stays() -> None:
    r = behavior_tenant_429_stays()
    assert r.passed, r.summary
    assert r.extra["noisy"] == 429
    assert r.extra["quiet"] == 200
    assert r.extra["overflow"] == 0

def test_behavior_ignore_stale_pod() -> None:
    r = behavior_ignore_stale_pod()
    assert r.passed, r.summary
    assert "B" not in r.extra["chosen"]
    assert r.extra["unknown"] >= 1

def test_behavior_overflow_on_503() -> None:
    r = behavior_overflow_on_503()
    assert r.passed, r.summary
    assert r.extra["overflow"] >= 1
    assert r.extra["via"] == "overflow"
    assert r.extra["happened"] == "overflow_ok"
    assert r.extra["reasons"]

def test_all_named_behaviors_are_wired() -> None:
    for name in BEHAVIORS:
        result = run_behavior(name, record=False)
        assert result.passed, result.summary
    assert run_behavior("B3", record=False).name == "tenant-429-stays"
    assert run_behavior("B10", record=False).name == "overflow-on-503"
