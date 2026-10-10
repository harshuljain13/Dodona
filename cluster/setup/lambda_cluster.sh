#!/usr/bin/env bash
# One-shot cluster bring-up — this is Step 13 in the README.
# Run on: GPU box. Wraps lambda_k3s_hami.sh (k3s + HAMi) followed by
# the three HAMi slices: one prefill, two decode. Apply one at a time.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
bash "$ROOT/setup/lambda_k3s_hami.sh"
kubectl apply -f "$ROOT/k8s/hami/hami-prefill.yaml"
kubectl apply -f "$ROOT/k8s/hami/hami-decode.yaml"
