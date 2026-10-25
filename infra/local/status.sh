#!/usr/bin/env bash
# What the fleet is actually doing, answered by asking it.
#
#   make fleet-runpod-status     (this script, run on the GPU box)
#
# "The ports are listening" is not the question. The questions are: is each
# worker serving the model you think, is it on its own card, how full is its KV
# cache right now, and is there exactly one fleet. Every one of those has been
# wrong at some point today while the ports looked fine.
#
# No `set -e`. Half of this is greps that legitimately find nothing, and a
# status command that exits on the first absence is worse than no status
# command — that bug cost an afternoon in workers.sh.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
[[ -f .env ]] && set -a && source .env && set +a

PORT_BASE="${PORT_BASE:-8100}"
LOGS="$ROOT/infra/metrics/local"

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m%s\033[0m\n' "$*"; }
bad()  { printf '    \033[1;31m%s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

problems=0

# ── the workers ────────────────────────────────────────────────────────────
say "workers"
i=0
for role in prefill decode-0 decode-1; do
  port=$(( PORT_BASE + i )); i=$(( i + 1 ))

  model=$(curl -sf -m 5 "localhost:$port/v1/models" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)

  if [[ -z "$model" ]]; then
    bad "$(printf ':%-5s %-9s NOT ANSWERING' "$port" "$role")"
    problems=$(( problems + 1 ))
    continue
  fi

  # How full the cache is, right now. This is the number admission reads, so a
  # status that omits it leaves out the only thing the policy cares about.
  kv=$(curl -sf -m 5 "localhost:$port/metrics" 2>/dev/null \
    | awk '/^vllm:kv_cache_usage_perc/ { v=$NF } END { printf "%.3f", v+0 }')

  restarts=$(grep -ac "^\[$role\] exited" "$LOGS/$role.log" 2>/dev/null)
  restarts=${restarts:-0}

  line=$(printf ':%-5s %-9s %-34s kv %-6s %s restarts' \
    "$port" "$role" "$model" "${kv:-?}" "$restarts")
  if (( restarts > 0 )); then
    bad "$line"
    problems=$(( problems + 1 ))
  else
    ok "$line"
  fi
done

# ── the control plane ──────────────────────────────────────────────────────
say "control plane"
if curl -sf -m 5 localhost:8080/v1/models >/dev/null 2>&1; then
  ok "gateway   :8080   answering"
else
  bad "gateway   :8080   NOT ANSWERING"
  problems=$(( problems + 1 ))
fi

hops=$(curl -sf -m 5 localhost:50051/metrics 2>/dev/null \
  | awk '/^orch_kv_transfer_total/ { v=$NF } END { printf "%d", v+0 }')
if curl -sf -m 5 localhost:50051/metrics >/dev/null 2>&1; then
  # Zero is the honest answer while decode re-prefills; it is not a failure.
  ok "mooncake  :50051  answering, ${hops:-0} hops recorded"
else
  bad "mooncake  :50051  NOT ANSWERING"
  problems=$(( problems + 1 ))
fi

for name in prometheus grafana; do
  port=9090; [[ "$name" == grafana ]] && port=31495
  if curl -sf -m 3 "localhost:$port" >/dev/null 2>&1; then
    ok "$(printf '%-9s :%-6s answering' "$name" "$port")"
  else
    note "$(printf '%-9s :%-6s not running — dashboards unavailable' "$name" "$port")"
  fi
done

# ── the cards ──────────────────────────────────────────────────────────────
say "GPUs"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null \
  | while IFS=, read -r idx used total; do
      procs=$(nvidia-smi -i "${idx// /}" --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c .)
      note "$(printf 'GPU %s  %6s / %s MiB   %s process(es)' "${idx// /}" "${used// /}" "${total// /}" "$procs")"
    done

# ── is there exactly one fleet ─────────────────────────────────────────────
say "processes"
sup=$(pgrep -fc "infra/local/workers.sh" 2>/dev/null); sup=${sup:-0}
vll=$(pgrep -fc "vllm serve" 2>/dev/null); vll=${vll:-0}
gpu_procs=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c .)

note "supervisors: $sup   vllm: $vll   on a GPU: $gpu_procs"
# One fleet is one parent plus five supervised children.
if (( sup > 7 )); then
  bad 'more than one fleet is running — fleet-runpod was started twice'
  note "  fix: make fleet-runpod-down, then make fleet-runpod"
  problems=$(( problems + 1 ))
fi
if (( vll > gpu_procs )); then
  bad "$(( vll - gpu_procs )) vllm process(es) not attached to a GPU — probably failed to bind a port"
  problems=$(( problems + 1 ))
fi

echo
if (( problems == 0 )); then
  say "healthy"
else
  say "$problems problem(s) — logs: make fleet-runpod-logs"
  exit 1
fi
