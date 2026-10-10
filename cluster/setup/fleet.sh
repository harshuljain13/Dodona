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

echo
echo "== the integrity check =="
kubectl get pods -o wide
nvidia-smi --query-compute-apps=pid,used_memory --format=csv

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
