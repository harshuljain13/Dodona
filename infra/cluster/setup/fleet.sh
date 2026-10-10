#!/usr/bin/env bash
# k3s + HAMi + KEDA, then the sliced fleet: one prefill and two decode.
# Run on: GPU box.
#
# Pods are applied ONE AT A TIME and waited on. Three vLLM pods contending for
# memory at startup can leave one with half the KV of its peers, and a fleet
# with unequal caches makes the pool-wide concurrency figure wrong.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_require_gpu.sh"

bash "$ROOT/setup/k3s.sh"

apply_and_wait() {
  local f="$1" dep="$2"
  echo "== $dep =="
  kubectl apply -f "$f"
  kubectl rollout status "deploy/$dep" --timeout=25m
}

apply_and_wait "$ROOT/k8s/fleet/vllm-prefill.yaml" vllm-prefill
apply_and_wait "$ROOT/k8s/fleet/vllm-decode.yaml"  vllm-decode

# The gateway goes in front of the fleet. Skip this and guardrails, admission,
# placement and the hop are bypassed on every request.
apply_and_wait "$ROOT/k8s/gateway/orch-serve.yaml" orch-serve

# orch-serve mounts the repo from the host rather than baking an image, so a
# sync changes the files on disk while the running process keeps the old code in
# memory. `kubectl apply` on an unchanged manifest is a no-op, so without this
# every control-plane edit silently does not take.
echo "== restarting orch-serve to pick up synced code =="
kubectl rollout restart deploy/orch-serve
kubectl rollout status deploy/orch-serve --timeout=5m

# Open WebUI as a pod, not a docker container: k3s owns this node, and `docker`
# here needs sudo. It points at orch-serve, so hand testing takes the same path.
apply_and_wait "$ROOT/k8s/ui/open-webui.yaml" open-webui

echo
echo "== the integrity check =="
kubectl get pods -o wide
nvidia-smi --query-compute-apps=pid,used_memory --format=csv

# The capacity checks, against a decode pod rather than a whole-card engine.
# Overhead does not extrapolate between the two — CUDA-graph capture scales
# with available memory — so the per-slice figure is the only usable one.
echo
echo "== KV connector =="
# The single fact that decides whether the hop moves bytes. Buried in a startup
# log otherwise, and "No RDMA devices found" is a silent downgrade to TCP.
for pool in prefill decode; do
  # `|| true` is load-bearing: grep exits 1 on no match, and under
  # `set -euo pipefail` that fails the substitution and kills the script. This
  # block aborted the whole bring-up before the capacity checks could run.
  # --tail=-1, not a window. The connector initialises once at pod startup and
  # is thousands of health-check lines back by the time anyone looks; a windowed
  # grep reports "did not initialise" for a connector that is running fine,
  # which is a worse answer than none.
  found=$(kubectl logs --tail=-1 -l "app=vllm-$pool" 2>/dev/null \
          | grep -iE "HCAs|Using .* transport|No RDMA devices|Mooncake Transfer Engine is using" \
          | tail -3 || true)
  if [[ -n "$found" ]]; then
    printf '  %-8s\n' "$pool"
    printf '    %s\n' "$found"
  else
    printf '  %-8s no connector lines in the full log — check --kv-transfer-config\n' "$pool"
  fi
done
cat <<'NOTE'
    >=1 HCAs + RDMA transport   the fast path
    0 HCAs + TCP transport      works, but /dev/infiniband is not in the pod
    no lines at all             no connector: decode re-prefills
NOTE

echo
echo "== capacity, measured on a slice =="
slice_mib=$(sed -n 's/.*nvidia\.com\/gpumem: *"\([0-9]*\)".*/\1/p' \
  "$ROOT/k8s/fleet/vllm-decode.yaml" | head -1)
pod_log=$(mktemp)
kubectl logs deploy/vllm-decode --tail=-1 >"$pod_log" 2>/dev/null
python3 "$ROOT/setup/gate.py" --port 30801 --log "$pod_log" --slice-mib "$slice_mib" \
  || echo "    capacity check failed — the arithmetic is wrong, not the cluster"
rm -f "$pod_log"

# A request that reached a pod without passing the gateway leaves orch_* at
# zero, which is the only way to tell the two apart from outside.
echo
echo "== does a request actually traverse the gateway? =="
before=$(curl -s localhost:8080/metrics | awk '/^orch_requests_total/ {print $2; exit}')
curl -s localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct-AWQ","messages":[{"role":"user","content":"say ok"}],"max_tokens":8}' \
  | head -c 400
echo
after=$(curl -s localhost:8080/metrics | awk '/^orch_requests_total/ {print $2; exit}')
echo "    orch_requests_total  ${before:-0} -> ${after:-0}"
[ "${after:-0}" != "${before:-0}" ] \
  && echo "    the gateway is in the path" \
  || echo "    WARNING: unchanged — the request did not go through the gateway"

cat <<'NOTE'

    Expect THREE vLLM processes on GPU index 0, none above its slice. The
    failure mode here is the inverse of a multi-card fleet's: not two pods
    landing on one card, but one pod exceeding its slice and quietly taking
    memory from another. If HAMi is not enforcing the cap, the run is void.

    Next: make observability
NOTE
