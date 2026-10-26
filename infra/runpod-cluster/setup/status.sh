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

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
[[ -f .env ]] && set -a && source .env && set +a

# GPUS, LAB_TOPOLOGY, WORKER_COUNT, NAMES, DEVICES, PORTS. Shared with
# workers.sh so this cannot report workers the fleet never started — it used to
# ask for `prefill decode-0 decode-1` whatever was actually running.
source "$(dirname "$0")/layout.sh"
LOGS="$ROOT/infra/metrics/local"

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m%s\033[0m\n' "$*"; }
bad()  { printf '    \033[1;31m%s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

problems=0

# ── the workers ────────────────────────────────────────────────────────────
say "workers"
note "$LAB_TOPOLOGY — $WORKER_COUNT engine(s) on $GPUS card(s)"
for i in $(seq 0 $(( WORKER_COUNT - 1 ))); do
  role="${NAMES[$i]}"; port="${PORTS[$i]}"

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

  # Uptime, not a log count. A log file records everything that ever happened
  # on this box; it said 51 restarts for a worker that had been serving
  # steadily for twenty minutes, because the crash loops of an earlier, broken
  # fleet were still in the file. What matters is whether THIS process is up
  # and has stayed up.
  pid=$(ss -lptnH "sport = :$port" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  up=$(ps -p "${pid:-0}" -o etimes= 2>/dev/null | tr -d ' ')
  up=${up:-0}

  if (( up >= 3600 )); then   age="$(( up / 3600 ))h"
  elif (( up >= 60 )); then   age="$(( up / 60 ))m"
  else                        age="${up}s"
  fi

  line=$(printf ':%-5s %-9s %-34s kv %-6s up %s' \
    "$port" "$role" "$model" "${kv:-?}" "$age")

  # Under two minutes and answering means it restarted very recently — which,
  # for a worker that takes minutes to load, means it is flapping.
  if (( up < 120 )); then
    bad "$line   (just restarted — may be flapping)"
    problems=$(( problems + 1 ))
  else
    ok "$line"
  fi
done

# History, reported as history. Worth seeing — a fleet that crashed thirty
# times before settling is worth knowing about — but it is not a fault now,
# and a status that exits non-zero for it cannot be used as a health check.
hist=0
for role in "${NAMES[@]}"; do
  n=$(grep -ac "^\[$role\] exited" "$LOGS/$role.log" 2>/dev/null); n=${n:-0}
  hist=$(( hist + n ))
done
if (( hist > 0 )); then
  note "logs record $hist restart(s) across this fleet's lifetime — make fleet-runpod-logs"
fi

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
    if [[ "$name" == grafana ]]; then
      ok "$(printf '%-9s :%-6s answering   login: admin / %s' "$name" "$port" "${GRAFANA_PASSWORD:-admin}")"
    else
      ok "$(printf '%-9s :%-6s answering' "$name" "$port")"
    fi
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
mapfile -t sup_pids < <(pgrep -f "infra/runpod-cluster/setup/workers.sh" 2>/dev/null || true)
sup=${#sup_pids[@]}
vll=$(pgrep -fc "vllm serve" 2>/dev/null); vll=${vll:-0}
gpu_procs=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c .)

# Count FLEETS, not processes.
#
# The old test was `supervisors > 7`, on the reasoning that one fleet is a
# parent plus five supervised children. Then the fleet grew a GPU exporter,
# prometheus and grafana — eight supervised components, so a single healthy
# fleet shows ten workers.sh processes and the check reported it as two. A
# status command that cries wolf on a fleet whose own smoke test passes is
# worse than no check: it sends you to `down` and back on a working fleet.
#
# A second fleet means a second TOP-LEVEL workers.sh — one whose parent is not
# itself a workers.sh. That stays true however many components get supervised.
fleets=0
for _p in "${sup_pids[@]:-}"; do
  [[ -n "$_p" ]] || continue
  _pp=$(ps -o ppid= -p "$_p" 2>/dev/null | tr -d ' ')
  _is_child=0
  for _q in "${sup_pids[@]}"; do
    if [[ "$_pp" == "$_q" ]]; then _is_child=1; break; fi
  done
  if (( _is_child == 0 )); then fleets=$(( fleets + 1 )); fi
done

note "fleets: $fleets   supervised processes: $sup   vllm: $vll   on a GPU: $gpu_procs"
if (( fleets > 1 )); then
  bad "$fleets fleets are running — fleet-runpod was started twice"
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
