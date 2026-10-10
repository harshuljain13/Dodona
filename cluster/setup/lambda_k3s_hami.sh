#!/usr/bin/env bash
# Install k3s with the NVIDIA default runtime, then the HAMi scheduler via Helm.
# Run on: GPU box. Idempotent — skips k3s if it is already installed.
# Leaves: KUBECONFIG at /etc/rancher/k3s/k3s.yaml and hami-scheduler ready.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
cd "$ROOT"

echo "== GPU =="
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

if [[ ! -x /usr/local/bin/k3s && ! -x /usr/bin/k3s ]]; then
  echo "== k3s (nvidia default runtime) =="
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode 644 --default-runtime nvidia" sh -
else
  echo "== k3s already installed =="
fi

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
if [[ ! -r "$KUBECONFIG" ]]; then
  echo "Cannot read $KUBECONFIG — re-run as a user who can, or sudo chmod 644."
  exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
  echo "== helm =="
  curl -sfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

NODE="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
kubectl label node "$NODE" gpu=on --overwrite

_helm_wait() {
  local ns="$1"
  shift
  echo "Helm is quiet until Ready. Printing $ns pods every 15s."
  (
    while true; do
      echo "-- $(date -u +%H:%M:%S) $ns --"
      kubectl -n "$ns" get pods 2>/dev/null || true
      sleep 15
    done
  ) &
  local watch=$!
  set +e
  "$@"
  local rc=$?
  kill "$watch" 2>/dev/null || true
  wait "$watch" 2>/dev/null || true
  set -e
  return "$rc"
}

echo "== HAMi (helm) =="
READY_SCHED="$(kubectl -n kube-system get deploy hami-scheduler -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
if [[ "${READY_SCHED:-0}" -ge 1 ]]; then
  echo "HAMi scheduler already Ready — skip helm upgrade"
else
  helm repo add hami-charts https://project-hami.github.io/HAMi/ >/dev/null
  helm repo update hami-charts >/dev/null
  K8S_VERSION="$(kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"].split("+")[0])')"
  _helm_wait kube-system helm upgrade --install hami hami-charts/hami \
    --version 2.9.0 \
    --namespace kube-system \
    --set scheduler.kubeScheduler.image.registry=registry.k8s.io \
    --set scheduler.kubeScheduler.image.repository=kube-scheduler \
    --set "scheduler.kubeScheduler.image.tag=${K8S_VERSION}" \
    --set "scheduler.kubeScheduler.imageTag=${K8S_VERSION}" \
    --set devicePlugin.deviceSplitCount=4 \
    --wait --timeout 10m
fi

echo "== KEDA (helm) =="
helm repo add kedacore https://kedacore.github.io/charts >/dev/null
helm repo update kedacore >/dev/null
_helm_wait keda helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace \
  --set prometheus.operator.enabled=true \
  --set prometheus.metricServer.enabled=true \
  --wait --timeout 10m

echo "== control plane =="
kubectl -n kube-system get pods | grep -E 'hami|NAME' || true
kubectl -n keda get pods

echo
echo "Cluster is up (k3s + HAMi + KEDA)."
echo "ScaledObjects are applied next; they start moving replicas on Day 2 when Prometheus is up."
echo "Next: bash setup/lambda_apply_slices.sh"
