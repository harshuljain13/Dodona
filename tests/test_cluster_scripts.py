from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_helm_and_grafana_are_not_on_the_harness() -> None:
    harness = (ROOT / "control" / "gateway" / "harness.py").read_text()
    assert "helm" not in harness
    assert "kubectl" not in harness
    guard = (ROOT / "cluster" / "setup" / "_require_gpu.sh").read_text()
    assert "Darwin" in guard
    assert "FakeWorker" in guard

def test_k3s_installs_keda_and_observability_installs_grafana() -> None:
    k3s = (ROOT / "cluster" / "setup" / "k3s.sh").read_text()
    assert "kedacore/keda" in k3s
    assert "grafana/grafana" not in k3s
    assert "helm upgrade --install grafana" not in k3s
    observability = (ROOT / "cluster" / "setup" / "observability.sh").read_text()
    assert "grafana" in observability.lower()
    assert "prometheus" in observability.lower()
    assert "evict" in observability.lower()
    prom = (ROOT / "cluster" / "k8s" / "observability" / "prometheus-values.yaml").read_text()
    assert "kube-state-metrics" in prom
    assert "prometheus-node-exporter:\n  enabled: true" in prom
    assert "job_name: keda" in prom
    assert "job_name: hami" in prom
    grafana = (ROOT / "cluster" / "k8s" / "observability" / "grafana-values.yaml").read_text()
    assert "sidecar" in grafana
    assert "grafana_dashboard" in grafana
    assert "helm upgrade --install grafana" in observability
    assert "dodona-dashboards" in observability
    assert "python3 -m observability.dashboards" in observability
    assert "dcgm-exporter" in observability
    assert "keda" in observability.lower()
    assert "hami" in observability.lower()
    assert "mooncake" in observability.lower()
    assert "docker compose" not in observability.lower()
