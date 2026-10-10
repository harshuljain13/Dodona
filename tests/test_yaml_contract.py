from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "cluster" / "k8s"

def _read_all() -> str:
    files = sorted(K8S.glob("*/*.yaml"))
    return "\n".join(p.read_text() for p in files)

def test_yaml_lives_with_the_owner() -> None:
    # Our inventory, not the lab's. httproute.yaml and inferencepool.yaml were
    # llm-d's Gateway API Inference Extension and are rejected; the hami/*
    # skeletons had no --model and were replaced by fleet/*.
    assert {p.name for p in (K8S / "gateway").glob("*.yaml")} == {"orch-serve.yaml"}
    assert {p.name for p in (K8S / "router").glob("*.yaml")} == {
        "keda-prefill.yaml",
        "keda-decode.yaml",
    }
    assert {p.name for p in (K8S / "fleet").glob("*.yaml")} == {
        "vllm-prefill.yaml",
        "vllm-decode.yaml",
    }
    assert not (K8S / "hami").exists() or not list((K8S / "hami").glob("*.yaml"))
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
    assert "vllm:kv_cache_usage_perc" in joined or 'orch_tokens_in_flight{phase="decode"}' in joined

def test_hami_and_keda_fields() -> None:
    text = _read_all()
    assert "hami-scheduler" in text
    assert "nvidia.com/gpumem" in text
    assert "nvidia.com/gpucores" in text
    assert "metricType: AverageValue" in text
    assert re.search(r"minReplicaCount:\s*1", text)
    # InferencePool + endpointPickerRef were llm-d's placement half. We removed
    # them deliberately — see PROJECT.md "Alternatives considered and rejected" —
    # so asserting their presence would lock in infrastructure we chose not to
    # run. What replaces them: our own fleet manifests, one prefill and two
    # decode, each pinned to a HAMi slice.
    assert "inference.networking.k8s.io" not in text, "EPP was rejected; see PROJECT.md"
    assert "mooncake" in text.lower()
    prefill = (K8S / "fleet" / "vllm-prefill.yaml").read_text()
    decode = (K8S / "fleet" / "vllm-decode.yaml").read_text()
    for name, y in (("prefill", prefill), ("decode", decode)):
        # Either form is fine; naming no model at all crash-loops the pod.
        assert "--model=" in y or "vllm serve Qwen/" in y, f"{name} must name a model"
        assert "type: Recreate" in y, f"{name} cannot RollingUpdate on a full GPU"
        assert "--tool-call-parser=hermes" in y, f"{name} needs the tool parser"
    # One of each at rest. KEDA adds the second decode under load, which is what
    # makes the scaler observable instead of installed-and-inert.
    assert "replicas: 1" in prefill and "replicas: 1" in decode

def test_mooncake_moves_real_kv_blocks() -> None:
    """The hop is bytes or it is bookkeeping. These are what make it bytes.

    Without --kv-transfer-config the decode pod re-prefills the whole prompt and
    the "hop" is a JSON row. Roles must be opposite, and the consumer has to be
    told where the producer's bootstrap server is or it never finds the blocks.
    """
    prefill = (K8S / "fleet" / "vllm-prefill.yaml").read_text()
    decode = (K8S / "fleet" / "vllm-decode.yaml").read_text()
    for name, y in (("prefill", prefill), ("decode", decode)):
        assert "--kv-transfer-config" in y, f"{name} has no connector: no blocks move"
        assert '"kv_connector":"MooncakeConnector"' in y, f"{name} names no connector"
        # Not in the vllm image; cuda13 because torch here is 2.13.0+cu130.
        assert "mooncake-transfer-engine-cuda13" in y, f"{name} never installs mooncake"
    assert '"kv_role":"kv_producer"' in prefill
    assert '"kv_role":"kv_consumer"' in decode
    assert '"bootstrap_server_address":"vllm-prefill:8998"' in decode
    assert "containerPort: 8998" in prefill, "bootstrap port must be exposed"
    assert "port: 8998" in prefill, "and reachable through the Service"
    assert "SERVE_HOST" in (K8S / "gateway" / "orch-serve.yaml").read_text()
    # Ours, from docs/project/01-capacity.md. The lab's VL-3B and its
    # tensor/pipeline/data-parallel flags are gone: TP=1 because 5.2 GiB of
    # weights fits one slice and HAMi slices are not independent HBM pools.
    assert "Qwen/Qwen2.5-7B-Instruct-AWQ" in prefill
    assert "--tensor-parallel-size" not in prefill, "TP=1; nothing to shard"
    assert "--max-model-len=16384" in prefill
    assert 'nvidia.com/gpumem: "12288"' in prefill
    # Must stay ABOVE the per-slice KV limit of ~4.9 sequences, so the cache runs
    # out before the scheduler and a KV-pressure policy can fire. The whole-card
    # figure (~73.5) is NOT the one that applies to a 12 GiB slice.
    assert "--max-num-seqs=64" in prefill
    assert "--max-num-seqs=64" in decode
