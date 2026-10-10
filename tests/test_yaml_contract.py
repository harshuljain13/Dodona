from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "infra" / "cluster" / "k8s"

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
        # Server-side panel rendering. Without it Grafana's /render returns 500
        # and a notebook can only link to a dashboard, not embed the panel.
        "renderer.yaml",
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


def test_deployments_holding_a_singleton_use_recreate() -> None:
    """A Deployment competing for a once-per-node resource cannot RollingUpdate.

    The default surges a replacement before retiring the old pod. When the pod
    holds something there is only one of — the GPU, or a hostPort — the
    replacement can never be scheduled, and the rollout does not fail fast: it
    sits at "1 old replicas are pending termination" until the progress
    deadline, minutes later.

    Both failures happened here: the fleet on nvidia.com/gpu, then orch-serve on
    hostPort 8080.

    DaemonSets are exempt — their rolling update terminates before it creates.
    """
    for path in sorted(K8S.glob("*/*.yaml")):
        text = path.read_text()
        if "kind: Deployment" not in text:
            continue
        singletons = [name for name, needle in
                      (("hostPort", "hostPort:"), ("nvidia.com/gpu", "nvidia.com/gpu:"))
                      if needle in text]
        if not singletons:
            continue
        assert "type: Recreate" in text, (
            f"{path.name} holds {', '.join(singletons)} but does not set "
            "strategy: Recreate — its rollout will deadlock")


def test_a_failed_kv_transfer_must_not_kill_the_pod() -> None:
    """kv_load_failure_policy=recompute, or a transfer failure is an outage.

    The default is `fail`: the request 500s, and then a late transfer
    completion arrives for a request the scheduler has already dropped,
    `assert req_id in self.requests` fires in _update_from_kv_xfer_finished,
    and the EngineCore dies. We watched a 10k-token pull return -1 and take the
    decode pod down twice.

    A KV hop is an optimisation. Losing one should cost latency, not the
    request and the pod.
    """
    import json
    import re

    for name in ("vllm-prefill", "vllm-decode"):
        text = (K8S / "fleet" / f"{name}.yaml").read_text()
        match = re.search(r"--kv-transfer-config '(\{.*?\})'", text, re.S)
        assert match, f"{name} has no --kv-transfer-config"
        config = json.loads(match.group(1))
        assert config.get("kv_load_failure_policy") == "recompute", (
            f"{name}: a failed transfer would fail the request and kill the engine")


def test_latency_buckets_resolve_the_latencies_we_actually_have() -> None:
    """vLLM's default request_latency buckets start at 0.3s.

    That family is shared by e2e, queue, inference, prefill and decode. Every
    request on this cluster finishes in under 100ms, so 96-100% of samples land
    in the first bucket, histogram_quantile interpolates inside [0, 0.3], and
    all five report 0.15/0.27/0.297 while their true means differ 17x —
    decode's mean is 5ms.

    The lowest boundary has to sit below the fastest thing being measured.
    """
    import json
    import re

    for name in ("vllm-prefill", "vllm-decode"):
        text = (K8S / "fleet" / f"{name}.yaml").read_text()
        match = re.search(r"--custom-histogram-buckets '(\{.*?\})'", text, re.S)
        assert match, f"{name}: no custom buckets, so e2e/queue/prefill/decode collide"
        buckets = json.loads(match.group(1))["request_latency"]
        assert buckets == sorted(buckets), f"{name}: bucket bounds must increase"
        assert buckets[0] <= 0.005, (
            f"{name}: lowest bound {buckets[0]}s is above decode's ~5ms mean")
        assert max(buckets) >= 30, f"{name}: nothing to catch a slow request"
