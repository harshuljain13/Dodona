#!/usr/bin/env bash
# Wipe the GPU box back to a bare machine. Run on: GPU box.
#
# Takes the GPU back in this order, because each step frees what the next needs:
#   1. our workloads      pods release their HAMi slices
#   2. helm releases      HAMi, KEDA, Prometheus, Grafana
#   3. k3s itself         removes containerd, CNI, every image
#   4. stray processes    a whole-card `vllm serve` started outside k3s
#
# KEEPS the HuggingFace cache. Those weights are ~5.6 GB per model and live on
# disk, not on the GPU; re-downloading them buys nothing. Pass --purge-models to
# drop them too.
set -euo pipefail

PURGE_MODELS=0
[[ "${1:-}" == "--purge-models" ]] && PURGE_MODELS=1

say() { printf '\n== %s ==\n' "$*"; }

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
kube() { sudo k3s kubectl "$@" 2>/dev/null || true; }

if sudo test -f /etc/rancher/k3s/k3s.yaml; then
  say "deleting our workloads"
  kube delete scaledobject --all
  kube delete deploy,svc -l app=vllm-prefill
  kube delete deploy,svc -l app=vllm-decode
  kube delete deploy,svc orch-serve open-webui
  # Pods hold their slices until they are gone, and HAMi will not release the
  # memory while a container is still terminating.
  kube wait --for=delete pod --all --timeout=120s

  say "uninstalling helm releases"
  for release in hami:kube-system keda:keda prometheus:monitoring grafana:monitoring; do
    sudo k3s kubectl -n "${release#*:}" get deploy >/dev/null 2>&1 || continue
    sudo helm uninstall "${release%%:*}" -n "${release#*:}" 2>/dev/null || true
  done
else
  say "no k3s kubeconfig — nothing deployed"
fi

say "uninstalling k3s"
if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
  sudo /usr/local/bin/k3s-uninstall.sh || true
else
  echo "    k3s-uninstall.sh not present — k3s was not installed this way"
fi

# Only our own. The pods' processes ran as root and are gone with k3s; [v] stops
# the pattern matching pkill's own command line.
say "stopping any whole-card engine"
pkill -u "$(id -u)" -f "[v]llm serve" || true
sleep 2

say "what still holds the GPU"
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv || true
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader || true

if (( PURGE_MODELS )); then
  say "removing the HuggingFace cache"
  rm -rf ~/.cache/huggingface
else
  echo
  echo "    HF cache kept at ~/.cache/huggingface  ($(du -sh ~/.cache/huggingface 2>/dev/null | cut -f1 || echo 0))"
  echo "    Drop it too:  bash cluster/setup/clean.sh --purge-models"
fi

cat <<'NOTE'

    The box is bare. Nothing should hold GPU memory above a few MiB.
    The instance is still running and still billing.

    Rebuild:  make cluster && make observability && make ssh
NOTE
