#!/usr/bin/env bash
# The fleet without an orchestrator: one vLLM per GPU, the gateway in front.
#
#   make workers        (this script, on the GPU box)
#
# Run on: the GPU box. The Kubernetes path is `make cluster`, and it needs a
# host — a machine where systemd is PID 1. Most rented GPUs are containers, so
# this exists to run the same control plane without k3s, HAMi or KEDA.
#
# What this replaces, manifest by manifest:
#
#   k8s/fleet/vllm-prefill.yaml    one engine.sh, pinned to GPU 0
#   k8s/fleet/vllm-decode.yaml     two more, pinned to the remaining GPUs
#   k8s/gateway/orch-serve.yaml    python -m gateway.serve
#   k8s/mooncake/mooncake.yaml     python -m router.mooncake
#   k8s/router/keda-*.yaml         nothing — scaling is minutes-slow and is
#                                  taught with that number rather than shown
#
# The Deployment controller's real job on a single node was start order,
# health gating and restart-on-crash. That is `supervise` below.
set -euo pipefail
set -m

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
[[ -f .env ]] && set -a && source .env && set +a

LOGS="$ROOT/infra/metrics/local"
mkdir -p "$LOGS"

PY="${PY:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3

# The Makefile exports PYTHONPATH — on the Mac. These processes run on the box,
# reached over ssh, and inherit none of it. Without the first line
# `python -m router.mooncake` is ModuleNotFoundError; without the second,
# engine.sh's bare `vllm` is "command not found" even though pip installed it
# into .venv/bin.
export PYTHONPATH="$ROOT/infra/control:$ROOT/infra/cluster${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$ROOT/.venv/bin:$PATH"

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# ── the card, and the slice we are pretending to have ──────────────────────
command -v nvidia-smi >/dev/null 2>&1 || fail "no nvidia-smi — this script runs on the GPU box."
mapfile -t GPU_MIB < <(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | tr -d ' ')
GPUS=${#GPU_MIB[@]}
(( GPUS > 0 )) || fail "nvidia-smi reports no GPUs."

# One worker per card when there are three. Fewer cards means sharing, which is
# what HAMi used to arbitrate — here it is just two processes on one device.
case "$GPUS" in
  1) DEVICES=(0 0 0) ;;
  2) DEVICES=(0 1 1) ;;
  *) DEVICES=(0 1 2) ;;
esac

# THE number that decides whether any of this is worth measuring.
#
# On Kubernetes, HAMi handed each pod a 12 GiB device and vLLM took 0.90 of
# that. Here each worker sees the whole card, so 0.90 of an 80 GiB A100 is a
# ~66 GiB KV pool, `kv_cache_usage_perc` never approaches the 0.90 the policy
# is thresholded at, and every run comes back INERT — numbers indistinguishable
# from having no admission policy at all.
#
# So the slice is reproduced by arithmetic: ask for SLICE_MIB of whatever card
# we are on, rather than a fixed fraction of an unknown one.
# SLICE_MIB=0 (the default) means "take the whole card", which is right when
# each worker has its own GPU. Capping only makes sense when workers SHARE a
# card, or when the model is small enough that a whole card gives it a KV pool
# so large the policy's threshold is unreachable.
#
# Getting this backwards fails in two opposite ways, both quiet:
#   too high — 0.90 of 80 GiB with a 7B is a ~66 GiB KV pool, the signal never
#              reaches 0.90, and every run comes back INERT
#   too low  — 12 GiB with a 32B is less than the weights, and vLLM will not
#              start at all
SLICE_MIB="${SLICE_MIB:-0}"
WHOLE_CARD_UTIL="${WHOLE_CARD_UTIL:-0.90}"

util_for() {
  local total="$1"
  if [[ "$SLICE_MIB" == "0" ]]; then
    printf '%s' "$WHOLE_CARD_UTIL"
    return
  fi
  awk -v s="$SLICE_MIB" -v t="$total" 'BEGIN { u = s / t; if (u > 0.92) u = 0.92; printf "%.3f", u }'
}

# ── the topology these processes form ──────────────────────────────────────
# Set here, not in .env: .env holds what a person chooses — the box, the keys,
# the model, the thresholds. Which URL a worker listens on is implied by the
# launcher, and leaving stale pool URLs in .env is how a cluster run ends up
# talking to localhost.
export PREFILL_URLS="http://127.0.0.1:8000"
export DECODE_URLS="http://127.0.0.1:8001,http://127.0.0.1:8002"
export LOCAL_BASE_URL="http://127.0.0.1:8000/v1"
export MOONCAKE_URL="http://127.0.0.1:50051"
export LAB_TOPOLOGY="${LAB_TOPOLOGY:-disaggregated}"
export ORCH_URL="${ORCH_URL:-http://127.0.0.1:8080/v1}"

PIDS=()
cleanup() {
  say "stopping"
  # Negative PID: the whole process group. vllm and prometheus both spawn
  # children, and killing the shell that started them leaves the ports bound.
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill -TERM "-$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Restart on crash, with a floor on how fast we retry. A worker that dies
# instantly and forever should say so rather than spin the CPU silently.
supervise() {
  local name="$1"; shift
  (
    local fails=0
    while true; do
      "$@" >>"$LOGS/$name.log" 2>&1 || true
      fails=$(( fails + 1 ))
      printf '[%s] exited (restart %d)\n' "$name" "$fails" >>"$LOGS/$name.log"
      # Once, with the reason. Repeating it every three seconds buries the one
      # line that says what actually went wrong.
      if (( fails == 3 )); then
        printf '\033[1;31m[%s] died 3 times. Last error:\033[0m\n' "$name" >&2
        grep -vE "^\[$name\] exited" "$LOGS/$name.log" | tail -3 | sed 's/^/    /' >&2
        printf '    still retrying — full log: %s/%s.log\n' "$LOGS" "$name" >&2
      fi
      # Back off, so a permanently broken worker is not a busy loop.
      sleep $(( fails < 3 ? 3 : 15 ))
    done
  ) &
  PIDS+=($!)
}

wait_for() {
  local name="$1" url="$2" tries="${3:-120}"
  for _ in $(seq "$tries"); do
    curl -sf -m 2 "$url" >/dev/null 2>&1 && { note "$name ready"; return 0; }
    sleep 2
  done
  fail "  $name never answered at $url — see $LOGS/$name.log"
}

# ── the hop ledger ─────────────────────────────────────────────────────────
# First, because kvbus POSTs to it on the very first hop. It records count,
# tokens and bytes; it does not move KV blocks, because decode re-prefills.
say "mooncake  —  :50051  (the hop ledger)"
supervise mooncake "$PY" -m router.mooncake
sleep 1

# ── the engines ────────────────────────────────────────────────────────────
start_worker() {
  local name="$1" port="$2" dev="$3"
  local util; util="$(util_for "${GPU_MIB[$dev]}")"
  local budget; budget=$(awk -v u="$util" -v t="${GPU_MIB[$dev]}" 'BEGIN { printf "%.0f", u * t }')
  note "$name  GPU $dev  ${GPU_MIB[$dev]} MiB card  util $util  ->  ${budget} MiB for weights + KV"
  # `env` rather than a VAR=x prefix: a prefix on a *function* call leaks into
  # the calling shell in bash, so the gateway started later would inherit
  # CUDA_VISIBLE_DEVICES from the last worker.
  supervise "$name" env \
    CUDA_VISIBLE_DEVICES="$dev" PORT="$port" UTIL="$util" \
    bash "$ROOT/infra/cluster/setup/engine.sh"
}

say "engines  —  $GPUS GPU(s), 1 prefill + 2 decode"
# Sequentially, not together: two processes profiling free memory on the same
# card at the same moment can each believe the other's allocation is available.
start_worker prefill  8000 "${DEVICES[0]}"
wait_for prefill  http://127.0.0.1:8000/health
start_worker decode-0 8001 "${DEVICES[1]}"
wait_for decode-0 http://127.0.0.1:8001/health
start_worker decode-1 8002 "${DEVICES[2]}"
wait_for decode-1 http://127.0.0.1:8002/health

# ── the control plane ──────────────────────────────────────────────────────
# Only once the pools answer: the gateway reads their /metrics to decide, and
# a gateway that starts first spends its first seconds deciding on absence.
say "gateway  —  :8080  (guard · admit · place · queue · hop)"
supervise gateway "$PY" -m gateway.serve
wait_for gateway http://127.0.0.1:8080/v1/models

# ── observability ──────────────────────────────────────────────────────────
# Optional on purpose. Without it the fleet still serves and the run still
# produces its bundle; you simply cannot watch it happen.
if command -v prometheus >/dev/null 2>&1; then
  say "prometheus  —  :9090"
  supervise prometheus prometheus \
    --config.file="$ROOT/infra/local/prometheus.yml" \
    --storage.tsdb.path="$LOGS/prometheus" \
    --web.listen-address=0.0.0.0:9090
else
  note "prometheus not installed — skipping. The fleet runs; the dashboards do not."
  note "  https://prometheus.io/download/  (untar, put it on PATH)"
fi

if command -v grafana-server >/dev/null 2>&1 || command -v grafana >/dev/null 2>&1; then
  say "grafana  —  :31495"
  supervise grafana env \
    DODONA_ROOT="$ROOT" \
    GF_PATHS_PROVISIONING="$ROOT/infra/local/grafana" \
    GF_SECURITY_ADMIN_PASSWORD="${GRAFANA_PASSWORD:-admin}" \
    GF_SERVER_HTTP_PORT=31495 \
    bash -c 'command -v grafana-server >/dev/null && exec grafana-server || exec grafana server'
else
  note "grafana not installed — skipping."
  note "  https://grafana.com/grafana/download  (untar, put it on PATH)"
fi

say "up"
note "gateway    http://127.0.0.1:8080/v1"
note "grafana    http://127.0.0.1:31495"
note "logs       $LOGS"
note ""
note "From the Mac:  make ssh   then open the URLs above."
note "Ctrl-C here stops everything."
wait
