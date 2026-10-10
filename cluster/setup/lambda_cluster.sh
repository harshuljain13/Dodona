#!/usr/bin/env bash
# Stage 2 — k3s + HAMi, then the three slices.
# Run on: GPU box.
#
# Pods are applied ONE AT A TIME and waited on. Three vLLM pods contending for
# memory at startup can leave one with half the KV of its peers, and a fleet
# with unequal caches makes the pool-wide concurrency figure wrong.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"

bash "$ROOT/setup/lambda_k3s_hami.sh"

apply_and_wait() {
  local f="$1" dep="$2"
  echo "== $dep =="
  kubectl apply -f "$f"
  kubectl rollout status "deploy/$dep" --timeout=25m
}

apply_and_wait "$ROOT/k8s/fleet/vllm-prefill.yaml" vllm-prefill
apply_and_wait "$ROOT/k8s/fleet/vllm-decode.yaml"  vllm-decode

echo
echo "== the integrity check =="
kubectl get pods -o wide
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
cat <<'NOTE'

    Expect THREE processes on GPU index 0, each ~10-11 GiB, none above its
    slice. The failure mode here is the inverse of a multi-card fleet's: not two
    pods landing on one card, but one pod exceeding its slice and quietly taking
    memory from another. If HAMi is not enforcing the cap, the run is void.

    Next: make observability        (stage 3)
NOTE
