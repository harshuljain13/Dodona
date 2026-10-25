#!/usr/bin/env bash
# Drive the GPU box from your Mac. Normally called by the Makefile.
#
#   up.sh cluster         k3s + HAMi + KEDA → fleet → gateway → capacity check
#   up.sh workers         the same fleet as processes — no k3s, for container boxes
#   up.sh observability   Prometheus + Grafana + DCGM
#   up.sh tunnel          forward the box's ports to this Mac
#   up.sh engine          ONE worker on the whole card, for comparison only
#   up.sh down            stop the engine and Open WebUI
#   up.sh logs [pool] [--follow] [--raw]   pod logs, scrape noise stripped
#   up.sh check           re-run the capacity checks against the whole-card engine
#   up.sh load [args]     run a load test ON THE BOX, pull the bundle back
#   up.sh kv              connector state and the transfer counters
#   up.sh clean           wipe the box back to bare metal
#
# Every remote step is one of the scripts next to this one, run over ssh. This
# file only sequences them — if a step is wrong, fix that script, not this one.

# Bash, not sh: arrays and [[ ]]. Under dash load_env is a syntax error.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "up.sh needs bash:  bash infra/cluster/setup/up.sh cluster   (or: make cluster)" >&2
  exit 1
fi
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"   # repo root, above infra/
cd "$PROJECT_ROOT"

REMOTE_DIR="dodona"        # where sync.sh puts the project on the box
ENGINE_LOG="engine.log"    # inside REMOTE_DIR
ENGINE_WAIT_TRIES=180      # x5s = 15 minutes, for weights plus CUDA graphs

announce() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail()     { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# ── the box ────────────────────────────────────────────────────────────────
# LAMBDA / LAMBDA_SSH_KEY are the older names and still work.
load_env() {
  [[ -f .env ]] || fail "No .env. Run:  cp .env.example .env   then set BOX and SSH_KEY"
  set -a; source .env; set +a

  BOX="${BOX:-${LAMBDA:-}}"
  SSH_KEY="${SSH_KEY:-${LAMBDA_SSH_KEY:-}}"
  # RunPod and most container hosts put sshd on a high port, not 22. Exported so
  # sync.sh and tunnel.sh, which are separate processes, see the same value.
  export SSH_PORT="${SSH_PORT:-22}"
  MODEL="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"

  [[ -n "$BOX"     ]] || fail "Set BOX in .env, e.g. BOX=ubuntu@203.0.113.4"
  [[ -n "$SSH_KEY" ]] || fail "Set SSH_KEY in .env to your private key path"
  [[ -f "$SSH_KEY" ]] || fail "No such key: $SSH_KEY"

  # Same reason: the two scripts below are spawned, not sourced.
  export BOX SSH_KEY

  SSH=(ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
}

# Run a command on the box, in the project directory.
on_box() { "${SSH[@]}" "$BOX" "cd ~/$REMOTE_DIR && $1"; }

# With a tty, or helm and kubectl output arrives in one lump at the end.
on_box_tty() { "${SSH[@]}" -t "$BOX" "cd ~/$REMOTE_DIR && $1"; }

# Our own processes only: the pods' run as root. [v] stops the pattern matching
# pkill's own command line, which would kill the remote shell and return 255.
stop_engine() {
  "${SSH[@]}" "$BOX" '{ pkill -u "$(id -u)" -f "[v]llm serve" || true; }; sleep 2'
}

# ── steps ──────────────────────────────────────────────────────────────────

sync_project() {
  announce "sync  —  Mac → $BOX:~/$REMOTE_DIR"
  bash infra/cluster/setup/sync.sh
}

# Asked before anything is synced or installed, because the answer invalidates
# the whole bring-up and the box cannot tell us later in any useful way: k3s
# fails two minutes in, naming systemd and not the container it is trapped in.
require_a_host() {
  local init
  init=$("${SSH[@]}" "$BOX" 'cat /proc/1/comm 2>/dev/null' | tr -d '\r')
  [[ "$init" == "systemd" ]] && return 0
  echo
  fail "  $BOX is a container, not a host — PID 1 is ${init:-unknown}.

  k3s installs as a systemd service and HAMi slices the GPU through the kernel.
  Neither works inside someone else's container, so nothing in this bring-up
  can succeed here.

  On RunPod that means a standard Pod. Book an Instant Cluster or a bare-metal
  instance instead — see PREREQUISITES.md step 6."
}

show_card() {
  announce "card"
  "${SSH[@]}" "$BOX" \
    'nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader' | sed 's/^/    /'
  local compute_cap
  compute_cap=$("${SSH[@]}" "$BOX" \
    'nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d " "')
  if [[ "$compute_cap" != "8.0" ]]; then
    echo "    NOTE: compute capability $compute_cap is not sm80, so fp8 works on this card"
    echo "          and int4 is no longer forced. docs/project/01-capacity.md needs redoing."
  fi
}

install_packages() {
  announce "packages  —  venv, requirements, vllm"
  on_box_tty 'bash infra/cluster/setup/box_packages.sh'
}

# Holds the whole card, to measure the model's real footprint before slicing.
start_engine() {
  announce "engine  —  $MODEL on :8000, whole card"
  stop_engine
  on_box "source .venv/bin/activate && \
    LOCAL_MODEL='$MODEL' setsid nohup bash infra/cluster/setup/engine.sh \
    </dev/null >$ENGINE_LOG 2>&1 & \
    sleep 1; echo '    started, logging to ~/$REMOTE_DIR/$ENGINE_LOG'"
}

wait_for_engine() {
  printf '    waiting for the engine'
  local try
  for try in $(seq 1 "$ENGINE_WAIT_TRIES"); do
    if "${SSH[@]}" "$BOX" 'curl -sf localhost:8000/v1/models >/dev/null' 2>/dev/null; then
      printf ' ready (%ss)\n' "$((try * 5))"
      return 0
    fi
    printf '.'; sleep 5
  done
  printf '\n'
  fail "Engine did not answer in 15 min.  bash infra/cluster/setup/up.sh logs"
}

check_capacity() {
  announce "capacity  —  checks against the running engine"
  on_box "python3 infra/cluster/setup/gate.py --log ~/$REMOTE_DIR/$ENGINE_LOG" || fail \
"    The capacity check failed, which means the arithmetic is wrong rather than
    the cluster. Read docs/project/01-capacity.md before going further."
}

start_fleet() {
  announce "fleet  —  k3s + HAMi + KEDA, then one prefill and one decode"
  # Harmless if nothing is running: the slices need every byte of the card, so
  # any whole-card engine from `up.sh engine` has to go first.
  stop_engine
  on_box_tty 'bash infra/cluster/setup/fleet.sh'
}

start_observability() {
  announce "observability  —  Prometheus + Grafana + DCGM"
  on_box_tty 'bash infra/cluster/setup/observability.sh'
}

open_tunnel() {
  announce "tunnel  —  ports forwarded to this Mac. Leave this window open."
  cat <<'PORTS'
    http://127.0.0.1:30030   Open WebUI   <- talk to it here
    http://127.0.0.1:31495   Grafana      admin / password from `make observability`
    http://127.0.0.1:8080    gateway      /metrics, /v1/chat/completions
    http://127.0.0.1:30800   vLLM prefill (30801 decode) — bypasses the gateway
PORTS
  exec bash infra/cluster/setup/tunnel.sh
}

# ── commands ───────────────────────────────────────────────────────────────

# No venv and no whole-card engine. The pods bring their own vLLM in the
# container image, and the capacity checks run against a pod inside fleet.sh
# because a whole-card measurement cannot be extrapolated to a slice anyway.
cmd_cluster() {
  require_a_host
  sync_project
  show_card
  start_fleet
  announce "cluster up"
  echo "    make observability   Prometheus + Grafana"
  echo "    make ssh             open the tunnel"
}

# One worker on the whole card, for the comparison measurement only. Needs the
# box venv, which is why it is not part of `make cluster`.
cmd_engine() {
  sync_project
  show_card
  install_packages
  start_engine
  wait_for_engine
  check_capacity
  announce "engine up"
  echo "    This holds the whole card. Run 'make cluster' to replace it with the fleet."
}

# The fleet without an orchestrator, for boxes that are containers — which is
# most rented GPUs. Deliberately does NOT call require_a_host: being a container
# is the reason this command exists.
#
# It installs vLLM, which `make cluster` does not need: there the pods bring
# their own in the image. Here the workers are processes on the box.
cmd_workers() {
  sync_project
  show_card
  install_packages
  announce "workers  —  1 vLLM per GPU, gateway, mooncake, observability"
  echo "    Ctrl-C stops everything. Leave this open."
  on_box_tty "$(workers_env) bash infra/local/workers.sh"
}

#: What the fleet needs from .env, carried to the box explicitly.
#:
#: sync.sh excludes .env on purpose — "the box has the code but not your
#: secrets" — so nothing in it reaches a worker. The first real run downloaded a
#: 7B because LOCAL_MODEL was unset and engine.sh fell back to its default,
#: while .env on the Mac said 32B. Every other choice was invisible the same
#: way, including SLICE_MIB, which decides whether a run is INERT.
#:
#: Named individually rather than shipping the file: the box is rented, and it
#: has no business holding a Deepgram key.
WORKERS_ENV=(
  LOCAL_MODEL TEXT_MODEL VISION_MODEL      # which model, and the guard allowlist
  SLICE_MIB WHOLE_CARD_UTIL                # the memory budget per worker
  PORT_BASE                                # a rented box is not empty
  MAX_MODEL_LEN MAX_NUM_SEQS SCHEDULING_POLICY
  KV_BACKEND LAB_TOPOLOGY LAB_SPLIT
  HF_TOKEN                                 # gated weights
)

# Build `env K=V ...` for the values that are actually set, quoted for the
# remote shell. Unset stays unset, so each script's own default still applies.
workers_env() {
  local out="env" name value
  for name in "${WORKERS_ENV[@]}"; do
    value="${!name:-}"
    [[ -n "$value" ]] && out+=" $name=$(printf %q "$value")"
  done
  printf '%s' "$out"
}

cmd_observability() {
  require_a_host
  sync_project
  start_observability
  announce "observability up"
  echo "    make ssh   then open http://127.0.0.1:31495"
}

# Only the whole-card engine. The pods are k3s's and `make cluster` owns them;
# `docker` on this box needs sudo anyway.
cmd_down() {
  announce "stopping the whole-card engine"
  "${SSH[@]}" "$BOX" '{ pkill -u "$(id -u)" -f "[v]llm serve" || true; }; echo stopped'
  echo "    Pods keep running:  sudo k3s kubectl delete -f infra/cluster/k8s/fleet/"
  echo "    The box is still billing. Stop the instance in your provider's console."
}

# Whatever is actually serving: the pods if the fleet is up, otherwise the
# whole-card engine's log. PRINTS AND EXITS by default so it can be piped into
# grep — a log you cannot pipe is a log you have to read by eye.
#
#   up.sh logs                        last 200 lines of prefill, then exit
#   up.sh logs decode                 the other pool
#   up.sh logs prefill --follow       tail it
cmd_logs() {
  local pool="prefill" follow=0 raw=0 arg
  for arg in "$@"; do
    case "$arg" in
      -f|--follow)    follow=1 ;;
      --raw)          raw=1 ;;
      prefill|decode) pool="$arg" ;;
    esac
  done
  local flags="--tail=400"
  (( follow )) && flags="-f --tail=120"
  # Health and metrics scrapes are ~95% of the volume and never the answer.
  # Unfiltered, 200 lines is 200 scrapes and the startup lines you actually
  # want have long scrolled away. --raw keeps everything.
  local strip='grep -vE "(GET /metrics|GET /health|GET /ping) HTTP"'
  (( raw )) && strip="cat"
  exec "${SSH[@]}" "$BOX" "
    if sudo test -f /etc/rancher/k3s/k3s.yaml && sudo k3s kubectl get deploy vllm-$pool >/dev/null 2>&1; then
      sudo k3s kubectl logs $flags -l app=vllm-$pool | $strip
    elif [ $follow -eq 1 ]; then
      tail -f ~/$REMOTE_DIR/$ENGINE_LOG | $strip
    else
      tail -400 ~/$REMOTE_DIR/$ENGINE_LOG | $strip
    fi"
}

# Load runs ON THE BOX, against 127.0.0.1:8080. Over the ssh tunnel your WAN
# round-trip lands inside every TTFT and concurrency serialises through one TCP
# stream, so a bundle taken from the Mac measures your internet, not the fleet.
cmd_load() {
  sync_project
  announce "load  —  on the box, against the gateway on 127.0.0.1:8080"
  on_box_tty "python3 -m benchmarking.load --url http://127.0.0.1:8080 $*"
  announce "pulling the bundle back"
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$BOX:~/$REMOTE_DIR/infra/metrics/" infra/metrics/
  echo "    bundles under infra/metrics/"
}

# Syncs first: this deletes things, so it must be the current script doing it.
cmd_clean() {
  sync_project
  announce "clean  —  deleting workloads, helm releases, k3s, stray engines"
  on_box_tty 'bash infra/cluster/setup/clean.sh'
}

# Everything about the KV connector, in one place. The init lines are at pod
# startup and scroll away within minutes; the counters are the live answer.
cmd_kv() {
  announce "KV connector"
  on_box '
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
    for pool in prefill decode; do
      echo "-- $pool --"
      sudo k3s kubectl logs --tail=-1 -l app=vllm-$pool 2>/dev/null \
        | grep -iE "mooncake|transfer_id|HCAs|Using .* transport|No RDMA" \
        | tail -6 || echo "   no connector lines"
    done
    echo "-- what the engine counts (decode) --"
    curl -s localhost:30801/metrics \
      | grep -E "^vllm:(external_prefix_cache|prompt_tokens_by_source)" \
      | sed "s/{engine=\"0\",model_name=\"[^\"]*\"/{/"
  '
  echo
  echo "    external_kv_transfer > 0  the hop is real"
  echo "    = 0 with queries > 0      connector consulted, returning nothing"
}

# Sync first: this runs the copy on the box, and a stale one checks nothing.
cmd_check() {
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    infra/cluster/setup/gate.py "$BOX:~/$REMOTE_DIR/infra/cluster/setup/gate.py"
  on_box "python3 infra/cluster/setup/gate.py --log ~/$REMOTE_DIR/$ENGINE_LOG"
}

main() {
  local command="${1:-}"
  case "$command" in
    cluster|workers|observability|tunnel|engine|down|logs|check|clean|load|kv) ;;
    -h|--help|"") sed -n '2,14p' "$0"; exit 0 ;;
    *) fail "unknown command: $command   (try: up.sh --help)" ;;
  esac
  load_env
  case "$command" in
    cluster)       cmd_cluster ;;
    workers)       cmd_workers ;;
    observability) cmd_observability ;;
    engine)        cmd_engine ;;
    tunnel)        open_tunnel ;;
    down)          cmd_down ;;
    logs)          shift; cmd_logs "$@" ;;
    check)         cmd_check ;;
    kv)            cmd_kv ;;
    clean)         cmd_clean ;;
    load)          shift; cmd_load "$@" ;;
  esac
}

main "$@"
