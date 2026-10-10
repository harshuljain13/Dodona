from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_helm_and_grafana_are_not_on_the_harness() -> None:
    harness = (ROOT / "control" / "gateway" / "harness.py").read_text()
    assert "helm" not in harness
    assert "kubectl" not in harness
    guard = (ROOT / "cluster" / "setup" / "_lambda_only.sh").read_text()
    assert "Darwin" in guard
    assert "FakeWorker" in guard

def test_day1_cluster_has_keda_not_grafana() -> None:
    day1 = (ROOT / "cluster" / "setup" / "lambda_k3s_hami.sh").read_text()
    assert "kedacore/keda" in day1
    assert "grafana/grafana" not in day1
    assert "helm upgrade --install grafana" not in day1
    day2 = (ROOT / "cluster" / "setup" / "day2_observability.sh").read_text()
    assert "grafana" in day2.lower()
    assert "prometheus" in day2.lower()
    assert "evict" in day2.lower()
    prom = (ROOT / "cluster" / "k8s" / "observability" / "prometheus-values.yaml").read_text()
    assert "kube-state-metrics" in prom
    assert "prometheus-node-exporter:\n  enabled: true" in prom
    assert "job_name: keda" in prom
    assert "job_name: hami" in prom
    grafana = (ROOT / "cluster" / "k8s" / "observability" / "grafana-values.yaml").read_text()
    assert "sidecar" in grafana
    assert "grafana_dashboard" in grafana
    assert "helm upgrade --install grafana" in day2
    assert "dodona-dashboards" in day2
    assert "python3 -m observability.dashboards" in day2
    assert "dcgm-exporter" in day2
    assert "keda" in day2.lower()
    assert "hami" in day2.lower()
    assert "mooncake" in day2.lower()
    assert "docker compose" not in day2.lower()
