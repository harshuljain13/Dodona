#!/usr/bin/env bash
# The fleet without an orchestrator: one vLLM per GPU, the gateway in front.
#
#   make fleet-runpod        (this script, on the GPU box)
#
# Run on: the GPU box, over ssh — `make fleet-runpod` does that for you.
#
# Processes, not pods. A rented GPU is usually a container, where k3s cannot
# install itself at all, so there is no orchestrator here: this script IS the
# Deployment controller, and the job it is doing is start order, health gating
# and restart-on-crash. That is `supervise` below.
#
# The Kubernetes version of this fleet lives on the `lambda-cluster` branch,
# with HAMi slices and KEDA. Nothing on this path needs systemd to be PID 1.
set -euo pipefail
set -m

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
[[ -f .env ]] && set -a && source .env && set +a

LOGS="$ROOT/infra/metrics/local"
mkdir -p "$LOGS"

# Fresh logs per fleet. They were appended to for ever, so `status` counted
# restarts from runs that ended hours ago and reported a healthy fleet as
# having failed 51 times. Previous content moves aside rather than vanishing.
# NOT fleet.log. up.sh redirects this process's stdout into it before exec'ing
# here, so renaming it moves the file out from under an open descriptor: the
# output keeps going to fleet.log.prev, fleet.log stays empty, and the command
# waiting on the Mac sees no progress at all and looks frozen.
for old in "$LOGS"/*.log; do
  [[ -e "$old" ]] || continue
  [[ "$(basename "$old")" == "fleet.log" ]] && continue
  mv -f "$old" "$old.prev" 2>/dev/null || true
done

PY="${PY:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3

# The Makefile exports PYTHONPATH — on the Mac. These processes run on the box,
# reached over ssh, and inherit none of it. Without the first line
# `python -m gateway.serve` is ModuleNotFoundError; without the second,
# engine.sh's bare `vllm` is "command not found" even though pip installed it
# into .venv/bin.
#
# One root: infra/control holds gateway, router, edge and kernels. It used to
# also list infra/lambda-cluster, for a `kernels` package that moved here when
# the Kubernetes path became its own branch.
export PYTHONPATH="$ROOT/infra/control${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$ROOT/.venv/bin:$PATH"

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# ── the shape of the fleet ─────────────────────────────────────────────────
# GPUS, LAB_TOPOLOGY, WORKER_COUNT, NAMES, DEVICES, PORTS — shared with
# status.sh and smoke.sh, because all three drifted apart when this was inline.
command -v nvidia-smi >/dev/null 2>&1 || fail "no nvidia-smi — this script runs on the GPU box."
source "$(dirname "$0")/layout.sh"
(( GPUS > 0 )) || fail "nvidia-smi reports no GPUs."
export LAB_TOPOLOGY

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

# Kept for the messages and for anything reading the old name.
PREFILL_PORT="${PORTS[0]}"

# Always succeeds, printing a name or nothing.
#
# Under `set -euo pipefail` the obvious version is a trap: grep returns 1 when
# a port is FREE, pipefail makes that the pipeline's status, and the command
# substitution carries it into an assignment that set -e then treats as fatal.
# The script exited before printing a single line — on a box where every port
# was fine, which is the one case it was supposed to wave through.
port_holder() {
  local port="$1" pid=""
  pid=$(ss -lptn "sport = :$port" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2) || true
  [[ -z "$pid" ]] && return 0
  ps -p "$pid" -o comm= 2>/dev/null || true
  return 0
}

check_ports() {
  local busy=0 holder
  for port in "${PORTS[@]}" 8080 50051; do
    holder="$(port_holder "$port")"
    if [[ -n "$holder" ]]; then
      note "port $port is held by: $holder"
      busy=1
    fi
  done
  (( busy == 0 )) && return 0
  echo
  fail "  Ports are in use before anything started.

  A rented box is not empty — RunPod's Jupyter template runs nginx on :8001,
  which is decode-0's port in the default layout.

  Move the block:   PORT_BASE=8100 make fleet-runpod
  Or stop whatever holds them, if it is a previous run of this script."
}

# ── the topology these processes form ──────────────────────────────────────
# Set here, not in .env: .env holds what a person chooses — the box, the keys,
# the model, the thresholds. Which URL a worker listens on is implied by the
# launcher, and leaving stale pool URLs in .env is how a cluster run ends up
# talking to localhost.
ALL_URLS=""
for _p in "${PORTS[@]}"; do
  ALL_URLS="${ALL_URLS:+$ALL_URLS,}http://127.0.0.1:$_p"
done
if [[ "$LAB_TOPOLOGY" == "aggregated" ]]; then
  # Every worker in BOTH lists. pools.py takes its one-pool branch on either
  # `topology == "aggregated"` or `decode_urls == prefill_urls`, and satisfying
  # both means the topology survives someone setting only one of them. The
  # pool's workers are then named engine-0..N, which is what the prometheus
  # labels below and `orch_replica_*{pod=...}` both use.
  export PREFILL_URLS="$ALL_URLS"
  export DECODE_URLS="$ALL_URLS"
else
  export PREFILL_URLS="http://127.0.0.1:${PORTS[0]}"
  export DECODE_URLS="http://127.0.0.1:${PORTS[1]},http://127.0.0.1:${PORTS[2]}"
fi
export LOCAL_BASE_URL="http://127.0.0.1:${PORTS[0]}/v1"
export MOONCAKE_URL="http://127.0.0.1:50051"
export LAB_TOPOLOGY="${LAB_TOPOLOGY:-disaggregated}"
export ORCH_URL="${ORCH_URL:-http://127.0.0.1:8080/v1}"

# The gateway's env, kept at parity with k8s/gateway/orch-serve.yaml.
#
# This is where the RunPod path kept diverging. The manifests set these two
# explicitly; this script did not, so the gateway fell back to the code defaults
# — which are deliberately small, for a capacity sweep, not for serving an agent:
#
#   MAX_PROMPT_CHARS       8,000 vs the manifests' 60,000. A research turn feeds
#                          a scraped page into the next call and dies on a 400
#                          prompt_too_long, after telling the listener it was
#                          looking things up.
#   TENANT_TOKENS_PER_MIN  10,000 vs the manifests' 50,000,000. A burst of
#                          twenty-four 700-token prompts came back as fifteen
#                          429s — a client-quota refusal wearing admission
#                          control's clothes, which is the one thing this fleet
#                          exists to measure honestly.
#
# Same numbers as the manifests, so a run on a rented box and a run on
# Kubernetes refuse work for the same reasons. Override either in .env.
export MAX_PROMPT_CHARS="${MAX_PROMPT_CHARS:-60000}"
export TENANT_TOKENS_PER_MIN="${TENANT_TOKENS_PER_MIN:-50000000}"

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

# Answering is not the same as being ours. nginx returned 2xx on /health and the
# gate reported "decode-0 ready" while vLLM was crash-looping behind it, so this
# asks /v1/models and insists on the OpenAI list payload only vLLM serves.
wait_for() {
  local name="$1" base="$2" tries="${3:-150}"
  for _ in $(seq "$tries"); do
    if curl -sf -m 2 "$base/v1/models" 2>/dev/null | grep -q '"object"'; then
      note "$name ready"
      return 0
    fi
    sleep 2
  done
  fail "  $name never served /v1/models at $base — see $LOGS/$name.log"
}

# ── the hop ledger ─────────────────────────────────────────────────────────
# First, because kvbus POSTs to it on the very first hop. It records count,
# tokens and bytes; it does not move KV blocks, because decode re-prefills.
check_ports

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
    bash "$ROOT/infra/setup/engine.sh"
}

if [[ "$LAB_TOPOLOGY" == "aggregated" ]]; then
  say "engines  —  $WORKER_COUNT on $GPUS GPU(s), each serving both phases"
  note "the router chooses between them; there is no hop to make"
else
  say "engines  —  $WORKER_COUNT on $GPUS GPU(s), 1 prefill + 2 decode"
  note "orchestration only — decode re-prefills, see docs/project/topology.md"
fi
# Sequentially, not together: two processes profiling free memory on the same
# card at the same moment can each believe the other's allocation is available.
for _i in $(seq 0 $(( WORKER_COUNT - 1 ))); do
  start_worker "${NAMES[$_i]}" "${PORTS[$_i]}" "${DEVICES[$_i]}"
  wait_for "${NAMES[$_i]}" "http://127.0.0.1:${PORTS[$_i]}"
done

# ── the control plane ──────────────────────────────────────────────────────
# Only once the pools answer: the gateway reads their /metrics to decide, and
# a gateway that starts first spends its first seconds deciding on absence.
say "gateway  —  :8080  (guard · admit · place · queue · hop)"
supervise gateway "$PY" -m gateway.serve
wait_for gateway "http://127.0.0.1:8080"

# ── observability ──────────────────────────────────────────────────────────
# Optional on purpose. Without it the fleet still serves and the run still
# produces its bundle; you simply cannot watch it happen.
# GPU telemetry, under the DCGM metric names the dashboards query. The
# Kubernetes path gets these from a dcgm-exporter DaemonSet, which a rented pod
# cannot run — so without this the `cluster` dashboard, the one that shows
# whether the card is busy, had no data at all.
#
# The mapping is built from DEVICES, the same array start_worker pinned each
# worker with, so the hardware dashboard says "GPU 1 · decode-0" rather than
# "GPU 1". Derived, not hardcoded: LAB_TOPOLOGY can reorder the cards, and a
# label that silently disagrees with the pinning is worse than no label.
#
# With fewer than three cards DEVICES repeats — (0 0 0) on one GPU, (0 1 1) on
# two — and more than one worker shares a card. Then no single worker name is
# the truth about that card, so it is labelled "shared" rather than whichever
# role happened to be assigned last.
declare -A _role_of=()
for _i in $(seq 0 $(( WORKER_COUNT - 1 ))); do
  _dev="${DEVICES[$_i]}"
  if [[ -n "${_role_of[$_dev]:-}" ]]; then
    _role_of[$_dev]="shared"
  else
    _role_of[$_dev]="${NAMES[$_i]}"
  fi
done
GPU_ROLES=""
for _dev in "${!_role_of[@]}"; do
  GPU_ROLES="${GPU_ROLES:+$GPU_ROLES,}${_dev}=${_role_of[$_dev]}"
done
say "gpu exporter  —  :9400  (DCGM_FI_DEV_* from nvidia-smi)"
note "roles: $GPU_ROLES"
supervise gpu-exporter "$PY" "$ROOT/infra/runpod-cluster/observability/gpu_exporter.py" \
  --port 9400 --roles "$GPU_ROLES"
sleep 1

if command -v prometheus >/dev/null 2>&1; then
  say "prometheus  —  :9090"
  # Render the template with the ports this fleet actually used. A static
  # config scrapes wherever it was written for, which is how every dashboard
  # came up empty while the workers were healthy on different ports.
  PROM_CFG="$LOGS/prometheus.yml"
  # Built from the fleet that actually started, not from three fixed ports.
  # `phase` is what the dashboards split on: prefill/decode when the pools are
  # split, `both` when every engine serves both — which is true, and better
  # than labelling a full engine "decode" to keep a panel company.
  VLLM_TARGETS=""
  for _i in $(seq 0 $(( WORKER_COUNT - 1 ))); do
    case "${NAMES[$_i]}" in
      prefill*)  _phase=prefill ;;
      decode*)   _phase=decode ;;
      *)         _phase=both ;;
    esac
    VLLM_TARGETS+="      - targets: [\"127.0.0.1:${PORTS[$_i]}\"]"$'\n'
    VLLM_TARGETS+="        labels: { phase: $_phase, pod: ${NAMES[$_i]} }"$'\n'
  done
  python3 - "$ROOT/infra/runpod-cluster/observability/prometheus.yml.tmpl" \
            "$PROM_CFG" "$VLLM_TARGETS" <<'RENDER'
import sys
tmpl, out, targets = sys.argv[1], sys.argv[2], sys.argv[3]
open(out, "w").write(open(tmpl).read().replace("__VLLM_TARGETS__\n", targets))
RENDER
  supervise prometheus prometheus \
    --config.file="$PROM_CFG" \
    --storage.tsdb.path="$LOGS/prometheus" \
    --web.listen-address=0.0.0.0:9090
else
  note "prometheus not installed — skipping. The fleet runs; the dashboards do not."
  note "  https://prometheus.io/download/  (untar, put it on PATH)"
fi

if command -v grafana-server >/dev/null 2>&1 || command -v grafana >/dev/null 2>&1; then
  say "grafana  —  :31495   login: admin / ${GRAFANA_PASSWORD:-admin}"
  # Build the provisioning tree Grafana actually expects: subdirectories named
  # dashboards/ and datasources/. Rendered under $LOGS so the repo copy stays a
  # template and the running config always has real paths in it.
  GF_PROV="$LOGS/grafana-provisioning"
  GF_DASH="$LOGS/grafana-dashboards"
  mkdir -p "$GF_PROV/dashboards" "$GF_PROV/datasources" "$GF_DASH"
  cp -f "$ROOT/infra/runpod-cluster/observability/grafana/datasources/prometheus.yml" "$GF_PROV/datasources/"

  # Four dashboards, generated here rather than copied from the k8s set.
  #
  # Copying and filtering was the old approach and it was the wrong shape: the
  # k8s dashboards are built for pods and slices, so even the seven that could
  # hold data described a cluster this is not. These four are written for this
  # topology and follow one request through it — gateway, engines, KV hop,
  # hardware — and every panel is checked against the metrics this fleet
  # actually exports by tests/test_runpod_dashboards.py.
  #
  # Generated at start, not checked in, so they cannot drift from the generator.
  rm -f "$GF_DASH"/*.json
  if "$PY" "$ROOT/infra/runpod-cluster/observability/dashboards.py" "$GF_DASH" >/dev/null; then
    note "dashboards: $(ls "$GF_DASH"/*.json 2>/dev/null | wc -l | tr -d ' ') generated — the request path, in order"
  else
    note "dashboards: generator failed; grafana will come up empty"
  fi

  sed "s|__DASHBOARDS__|$GF_DASH|" \
    "$ROOT/infra/runpod-cluster/observability/grafana/dashboards/dodona.yml.tmpl" \
    > "$GF_PROV/dashboards/dodona.yml"

  # Land on step 1 of the path instead of Grafana's stock welcome page, which
  # is a wall of links to documentation and makes a provisioned fleet look
  # unprovisioned for the first ten seconds of a demo.
  supervise grafana env \
    GF_PATHS_PROVISIONING="$GF_PROV" \
    GF_SECURITY_ADMIN_PASSWORD="${GRAFANA_PASSWORD:-admin}" \
    GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH="$GF_DASH/gateway.json" \
    GF_SERVER_HTTP_PORT=31495 \
    bash -c 'command -v grafana-server >/dev/null && exec grafana-server || exec grafana server'

  # Grafana keeps its dashboards in a database that outlives this script, and it
  # only reaps a stale one if the same provider inserted it. A box that once ran
  # with all eleven provisioned keeps the four Kubernetes ones for ever —
  # exactly the blank panels the filter above exists to prevent. The
  # provisioning directory is the source of truth, so delete whatever is in
  # Grafana and no longer on disk. A fresh box prunes nothing.
  for _ in $(seq 1 20); do
    curl -sf -m 2 -u "admin:${GRAFANA_PASSWORD:-admin}" \
      localhost:31495/api/health >/dev/null 2>&1 && break
    sleep 1
  done
  keep="$(ls "$GF_DASH"/*.json 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's|\.json$||')"
  pruned=0
  for u in $(curl -sf -m 5 -u "admin:${GRAFANA_PASSWORD:-admin}" \
               "localhost:31495/api/search?type=dash-db" 2>/dev/null \
             | tr ',' '\n' | grep -o '"uid":"[^"]*"' | cut -d: -f2 | tr -d '"'); do
    if ! printf '%s\n' "$keep" | grep -qx "${u#dodona-}"; then
      curl -sf -m 5 -u "admin:${GRAFANA_PASSWORD:-admin}" \
        -X DELETE "localhost:31495/api/dashboards/uid/$u" >/dev/null 2>&1 \
        && pruned=$((pruned + 1))
    fi
  done
  # An `[[ ... ]] && note` here would return 1 when there is nothing to prune,
  # and under `set -e` that is the whole script exiting on the happy path.
  if [[ "$pruned" -gt 0 ]]; then
    note "pruned $pruned stale dashboard(s) left by an earlier run"
  fi
else
  note "grafana not installed — skipping."
  note "  https://grafana.com/grafana/download  (untar, put it on PATH)"
fi

say "up"
note "gateway    http://127.0.0.1:8080/v1"
_report=""
for _i in $(seq 0 $(( WORKER_COUNT - 1 ))); do
  _report="${_report:+$_report  }${PORTS[$_i]} (${NAMES[$_i]})"
done
note "workers    $_report"
note "grafana    http://127.0.0.1:31495"
note "logs       $LOGS"
note ""
note "From the Mac:  make fleet-runpod-ssh   then open the URLs above."
note "Ctrl-C here stops everything."
wait
