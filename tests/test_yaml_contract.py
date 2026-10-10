from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s-config"

def _read_all() -> str:
    files = sorted(K8S.glob("*/*.yaml"))
    return "\n".join(p.read_text() for p in files)

def test_yaml_lives_with_the_owner() -> None:
    assert {p.name for p in (K8S / "gateway").glob("*.yaml")} == {
        "orch-serve.yaml",
        "httproute.yaml",
    }
    assert {p.name for p in (K8S / "router").glob("*.yaml")} == {
        "keda-prefill.yaml",
        "keda-decode.yaml",
        "inferencepool.yaml",
    }
    assert {p.name for p in (K8S / "hami").glob("*.yaml")} == {
        "hami-lambda.yaml",
        "hami-prefill.yaml",
        "hami-decode.yaml",
    }
    assert {p.name for p in (K8S / "mooncake").glob("*.yaml")} == {"mooncake.yaml"}
    assert {p.name for p in (K8S / "ui").glob("*.yaml")} == {
        "open-webui.yaml",
    }
    webui = (K8S / "ui" / "open-webui.yaml").read_text()
    assert "orch-serve:8080" in webui
    assert "litellm" not in webui
    assert {p.name for p in (K8S / "observability").glob("*.yaml")} == {
        "grafana-values.yaml",
        "prometheus-values.yaml",
        "dcgm-exporter.yaml",
    }
    assert {p.stem for p in (K8S / "observability" / "dashboards").glob("*.json")} == {
        "overview",
        "gateway",
        "router",
        "replicas",
        "vllm",
        "keda",
        "hami",
        "mooncake",
        "cluster",
        "errors",
    }

def test_two_scaled_objects_two_targets_two_queries() -> None:
    text = _read_all()
    kinds = re.findall(r"^kind:\s*(\S+)", text, flags=re.M)
    assert kinds.count("ScaledObject") == 2
    names = re.findall(r"scaleTargetRef:\n\s+name:\s+(\S+)", text)
    assert len(names) == 2
    assert len(set(names)) == 2
    queries = re.findall(r"query:\s*(.+)", text)
    queries = [q.strip().strip('"').strip("'") for q in queries]
    assert len(queries) == 2
    assert len(set(queries)) == 2
    joined = "\n".join(queries)
    assert 'orch_tokens_in_flight{phase="prefill"}' in joined
    assert "vllm:gpu_cache_usage_perc" in joined or 'orch_tokens_in_flight{phase="decode"}' in joined

def test_hami_and_keda_fields() -> None:
    text = _read_all()
    assert "hami-scheduler" in text
    assert "nvidia.com/gpumem" in text
    assert "nvidia.com/gpucores" in text
    assert "metricType: AverageValue" in text
    assert re.search(r"minReplicaCount:\s*1", text)
    assert "inference.networking.k8s.io/v1" in text
    assert "endpointPickerRef:" in text
    assert "extensionRef:" not in text
    assert "mooncake" in text.lower()
    assert "--model" in (K8S / "hami" / "hami-lambda.yaml").read_text()
    assert "SERVE_HOST" in (K8S / "gateway" / "orch-serve.yaml").read_text()
    hami = (K8S / "hami" / "hami-lambda.yaml").read_text()
    assert "Qwen/Qwen2.5-VL-3B-Instruct" in hami
    assert "VLLM_USE_V1" not in hami
    assert hami.count("--tensor-parallel-size") == 2
    assert hami.count("--pipeline-parallel-size") == 2
    assert hami.count("--data-parallel-size") == 2
    assert "--speculative-config" not in hami
