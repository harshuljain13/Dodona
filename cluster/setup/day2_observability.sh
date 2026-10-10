#!/usr/bin/env bash
# Day 2: install Prometheus and Grafana into the cluster with Helm.
# Run on: GPU box, after the fleet is up. Creates the monitoring namespace with
# persistence and alerting off, so it is disposable lab observability.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
cd "$ROOT"

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

echo "== Prometheus (helm) =="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update prometheus-community >/dev/null
helm upgrade --install prometheus prometheus-community/prometheus \
  --namespace monitoring \
  --create-namespace \
  --values "$ROOT/k8s-config/observability/prometheus-values.yaml" \
  --wait --timeout 10m

echo "== Grafana (helm) =="
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update grafana >/dev/null
helm upgrade --install grafana grafana/grafana \
  --namespace monitoring \
  --values "$ROOT/k8s-config/observability/grafana-values.yaml" \
  --wait --timeout 10m

echo "== DCGM exporter (cluster GPU) =="
kubectl apply -f "$ROOT/k8s-config/observability/dcgm-exporter.yaml" || true

echo "== Grafana dashboards (sidecar ConfigMap) =="
python3 -m observability.dashboards
kubectl -n monitoring create configmap class10-dashboards \
  --from-file="$ROOT/k8s-config/observability/dashboards" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n monitoring label configmap class10-dashboards grafana_dashboard=1 --overwrite

echo
echo "Day 2 is up. On the Mac (Step 2 tunnel must stay open) open only 127.0.0.1:"
echo "  Open WebUI:  http://127.0.0.1:30030   (no login)"
echo "  Grafana:     http://127.0.0.1:31495   user admin, password printed below"
echo "  Locust UI:   http://127.0.0.1:8089    after: locust -f app/locustfile.py --host http://127.0.0.1:8080"
echo "  Locust host: http://127.0.0.1:8080"
echo "  Crew flood:  ORCH_URL=http://127.0.0.1:8080/v1 python -m app.crew_flood --rounds 48 --workers 8"
echo "Generate load, then walk Grafana in this order:"
echo "  1. Cluster — node-exporter CPU/mem, kube-state pods, cAdvisor, DCGM GPU."
echo "  2. Success and failures — requests vs completed vs shed (429/503) vs overflow."
echo "  3. Overview (metrics.py) — orch_* from GET /metrics."
echo "  4. Gateway + admission — admit, shed, place, gateway-stage histograms."
echo "  5. Router — pick / sticky / unknown snapshot / overflow / planner / KV hop."
echo "  6. KEDA — scaler value/active vs kube replicas vs orch_tokens_in_flight."
echo "  7. HAMi — device memory/cores vs nvidia.com/gpumem slice limits."
echo "  8. Mooncake — hops / blocks / hop tokens vs orch_kv_transfer_*."
echo "  9. Pods and replicas — kube-state-metrics + orch_replica_*."
echo " 10. vLLM — gpu_cache, running/waiting, TTFT / ITL / e2e, prefix cache."
echo " 11. KV eviction: flood until the slice is hot, then evict — BlockRemoved"
echo "     must drop the prefix from kvbus or the Router routes on a ghost cache."
echo " 12. nvidia-smi on the node vs DCGM framebuffer vs HAMi gpumem inside the pod."
echo
echo "Grafana NodePort + admin password:"
kubectl -n monitoring get svc grafana
kubectl -n monitoring get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d
echo
echo "Prometheus: kubectl -n monitoring port-forward svc/prometheus-server 9090:80"
echo "Do not reimplement epp.py. Point the scraper at this build."
