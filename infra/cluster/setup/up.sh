#!/usr/bin/env bash
# Drive the GPU box from your Mac. Normally called by the Makefile.
#
#   up.sh cluster         k3s + HAMi + KEDA → fleet → gateway → capacity check
#   up.sh observability   Prometheus + Grafana + DCGM
#   up.sh tunnel          forward the box's ports to this Mac
#   up.sh engine          ONE worker on the whole card, for comparison only
#   up.sh down            stop the engine and Open WebUI
#   up.sh logs [prefill|decode]   follow the pods, or the whole-card engine
#   up.sh check           re-run the capacity checks against the whole-card engine
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
  MODEL="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"

  [[ -n "$BOX"     ]] || fail "Set BOX in .env, e.g. BOX=ubuntu@203.0.113.4"
  [[ -n "$SSH_KEY" ]] || fail "Set SSH_KEY in .env to your private key path"
  [[ -f "$SSH_KEY" ]] || fail "No such key: $SSH_KEY"

  SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
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
    http://127.0.0.1:3000    Open WebUI   <- talk to it here
    http://127.0.0.1:8080    gateway
    http://127.0.0.1:30800   vLLM prefill
    http://127.0.0.1:31495   Grafana
PORTS
  exec bash infra/cluster/setup/tunnel.sh
}

# ── commands ───────────────────────────────────────────────────────────────

# No venv and no whole-card engine. The pods bring their own vLLM in the
# container image, and the capacity checks run against a pod inside fleet.sh
# because a whole-card measurement cannot be extrapolated to a slice anyway.
cmd_cluster() {
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

cmd_observability() {
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

# Follows whatever is actually serving: the pods if the fleet is up, otherwise
# the whole-card engine's log. $1 picks a pool, default prefill.
cmd_logs() {
  local pool="${1:-prefill}"
  exec "${SSH[@]}" -t "$BOX" "
    if sudo test -f /etc/rancher/k3s/k3s.yaml &&        sudo k3s kubectl get deploy vllm-$pool >/dev/null 2>&1; then
      sudo k3s kubectl logs -f -l app=vllm-$pool --tail=60
    else
      tail -f ~/$REMOTE_DIR/$ENGINE_LOG
    fi"
}

# Syncs first: this deletes things, so it must be the current script doing it.
cmd_clean() {
  sync_project
  announce "clean  —  deleting workloads, helm releases, k3s, stray engines"
  on_box_tty 'bash infra/cluster/setup/clean.sh'
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
    cluster|observability|tunnel|engine|down|logs|check|clean) ;;
    -h|--help|"") sed -n '2,14p' "$0"; exit 0 ;;
    *) fail "unknown command: $command   (try: up.sh --help)" ;;
  esac
  load_env
  case "$command" in
    cluster)       cmd_cluster ;;
    observability) cmd_observability ;;
    engine)        cmd_engine ;;
    tunnel)        open_tunnel ;;
    down)          cmd_down ;;
    logs)          cmd_logs "${2:-}" ;;
    check)         cmd_check ;;
    clean)         cmd_clean ;;
  esac
}

main "$@"
