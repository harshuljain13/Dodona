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


def test_we_do_not_pass_a_flag_this_vllm_rejects() -> None:
    """--custom-histogram-buckets does not exist in vLLM 0.31.0.

    It would fix the real problem — the default request_latency buckets start
    at 0.3s and cannot resolve anything this cluster does — but it was added
    after 0.31.0 and passing it is a hard startup failure:

        vllm: error: unrecognized arguments: --custom-histogram-buckets

    I added it from reading vLLM's `main` branch without checking the version
    we actually run. Until the engine is upgraded, the percentiles for e2e,
    queue, prefill and decode are unusable and the mean is the honest figure.
    """
    for name in ("vllm-prefill", "vllm-decode"):
        text = (K8S / "fleet" / f"{name}.yaml").read_text()
        # Split on the YAML KEY, not the substring: the comment above it says
        # "everything below `args:`", and splitting on "args:" matched that
        # instead — so the test read the explanation as if it were the command.
        args = text.split("\n          args:\n")[1].split("ports:")[0]
        assert "--custom-histogram-buckets" not in args, (
            f"{name} passes a flag vLLM 0.31.0 rejects — the pod will crash-loop")


def test_no_comment_inside_a_folded_shell_command() -> None:
    """A `#` in a YAML folded scalar is not a comment — it is text.

    `args: [- >- ...]` folds every line into ONE shell command. A `#` survives
    the fold, and the shell then treats it as a comment and discards everything
    after it on that line. Putting an explanation there silently dropped
    --enable-auto-tool-choice, --host, --port and --kv-transfer-config: the pod
    came up healthy, served requests, and had no KV connector at all.

    Explanations belong above `args:`, where YAML really does strip them.
    """
    for path in sorted(K8S.glob("*/*.yaml")):
        text = path.read_text()
        if "- >-" not in text:
            continue
        for block in text.split("- >-")[1:]:
            # The folded scalar runs until a line that is less indented.
            lines = []
            for line in block.splitlines()[1:]:
                if line.strip() and not line.startswith(" " * 14):
                    break
                lines.append(line)
            folded = "\n".join(lines)
            assert "#" not in folded, (
                f"{path.name}: '#' inside a folded shell command — the shell will "
                f"discard the rest of the line:\n{folded[:200]}")


def test_the_engine_flags_that_must_never_be_lost() -> None:
    """Each of these has failed silently here at least once.

    A missing flag does not crash the pod. It comes up, answers requests, and
    quietly does less than you think it is doing.
    """
    required = {
        "--enable-prefix-caching": "no prefix cache, every prompt recomputed",
        "--enable-auto-tool-choice": "every OpenAI client sends tool_choice and gets a 400",
        "--tool-call-parser": "tool calls arrive as prose in message.content",
        "--kv-transfer-config": "no KV connector, decode re-prefills",
        "--custom-histogram-buckets": "latency percentiles collapse to one bucket edge",
        "--max-model-len": "context defaults, and the capacity arithmetic is wrong",
    }
    for name in ("vllm-prefill", "vllm-decode"):
        text = (K8S / "fleet" / f"{name}.yaml").read_text()
        for flag, consequence in required.items():
            assert flag in text, f"{name} lost {flag}: {consequence}"


def test_the_tenant_quota_cannot_bind_at_the_loads_we_measure() -> None:
    """The quota must sit above the heaviest sweep step, or it becomes the result.

    03-capacity's top step asks for ~2.58M tokens/min. With the quota at 2M, that
    step is refused with 429s and the notebook sees a cluster that "ran out" —
    when nothing about the GPU was involved. A 429 is client-attributable and
    says nothing about capacity, so a quota below the test load silently turns a
    capacity experiment into a rate-limit experiment.

    This caught a sweep that concluded "capacity is 2 concurrent sessions" on an
    H100.
    """
    import re

    text = (K8S / "gateway" / "orch-serve.yaml").read_text()
    match = re.search(r'TENANT_TOKENS_PER_MIN, value: "(\d+)"', text)
    assert match, "the quota is not set, so the gateway falls back to 10,000"
    quota = int(match.group(1))

    # The heaviest step in 03-capacity: 48 sessions x --repeat 8 (32 turns each),
    # ~13.76M tokens held over a ~90s window. Recomputed when --repeat was added
    # to make each step span several 15s Prometheus scrapes; the old figure
    # assumed a single 4-turn pass and understated demand eightfold.
    heaviest_per_min = 9_176_320
    assert quota == 0 or quota > heaviest_per_min * 2, (
        f"quota {quota:,}/min is not clear of the {heaviest_per_min:,}/min the "
        f"sweep demands — capacity results will contain 429s")


def test_every_make_target_is_phony() -> None:
    """A target named after a directory silently does nothing.

    `make app` reported "`app' is up to date" and ran nothing, because app/
    exists and make treats the target as a file that is already built. It fails
    by succeeding, which is the worst way for a command to fail.
    """
    import re

    makefile = (ROOT / "Makefile").read_text()
    phony = set()
    for line in makefile.splitlines():
        if line.startswith(".PHONY:"):
            phony.update(line.split(":", 1)[1].split())

    targets = {
        m.group(1)
        for m in re.finditer(r"^([a-z][a-z0-9-]*):(?!=)", makefile, re.M)
    }
    missing = sorted(targets - phony)
    assert not missing, (
        f"not declared .PHONY: {missing} — any of these that matches a file or "
        f"directory name will silently do nothing")
