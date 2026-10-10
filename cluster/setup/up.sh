#!/usr/bin/env bash
# Drive the GPU box from your Mac. Normally called by the Makefile.
#
#   up.sh cluster         engine → capacity check → k3s + HAMi + KEDA → fleet
#   up.sh observability   Prometheus + Grafana + DCGM
#   up.sh tunnel          forward the box's ports to this Mac
#   up.sh down            stop the engine and Open WebUI
#   up.sh logs            tail the engine log
#   up.sh check           re-run the capacity checks against a running engine
#
# Every remote step is one of the scripts next to this one, run over ssh. This
# file only sequences them — if a step is wrong, fix that script, not this one.

# Bash, not sh: arrays and [[ ]]. Under dash load_env is a syntax error.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "up.sh needs bash:  bash cluster/setup/up.sh cluster   (or: make cluster)" >&2
  exit 1
fi
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
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
  bash cluster/setup/sync.sh
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
  on_box_tty 'bash cluster/setup/box_packages.sh'
}

# Holds the whole card, to measure the model's real footprint before slicing.
start_engine() {
  announce "engine  —  $MODEL on :8000, whole card"
  stop_engine
  on_box "source .venv/bin/activate && \
    LOCAL_MODEL='$MODEL' setsid nohup bash cluster/setup/engine.sh \
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
  fail "Engine did not answer in 15 min.  bash cluster/setup/up.sh logs"
}

check_capacity() {
  announce "capacity  —  four checks against the running engine"
  on_box "python3 cluster/setup/gate.py --log ~/$REMOTE_DIR/$ENGINE_LOG" || fail \
"    The capacity check failed, which means the arithmetic is wrong rather than
    the cluster. Read docs/project/01-capacity.md before going further."
}

start_webui() {
  announce "Open WebUI  —  :3000"
  "${SSH[@]}" "$BOX" "docker rm -f open-webui >/dev/null 2>&1 || true; \
    docker run -d --name open-webui -p 3000:8080 \
      -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 \
      -e OPENAI_API_KEY=dummy -e WEBUI_AUTH=False \
      --add-host=host.docker.internal:host-gateway \
      -v open-webui:/app/backend/data \
      ghcr.io/open-webui/open-webui:main >/dev/null && echo '    started'"
}

start_fleet() {
  announce "fleet  —  k3s + HAMi + KEDA, then one prefill and two decode"
  echo "    The whole-card engine stops first: the slices need all of the card."
  stop_engine
  on_box_tty 'bash cluster/setup/fleet.sh'
}

start_observability() {
  announce "observability  —  Prometheus + Grafana + DCGM"
  on_box_tty 'bash cluster/setup/observability.sh'
}

open_tunnel() {
  announce "tunnel  —  ports forwarded to this Mac. Leave this window open."
  cat <<'PORTS'
    http://127.0.0.1:3000    Open WebUI   <- talk to it here
    http://127.0.0.1:8080    gateway
    http://127.0.0.1:30800   vLLM prefill
    http://127.0.0.1:31495   Grafana
PORTS
  exec bash cluster/setup/tunnel.sh
}

# ── commands ───────────────────────────────────────────────────────────────

cmd_cluster() {
  sync_project
  show_card
  install_packages
  start_engine
  wait_for_engine
  check_capacity
  start_webui
  start_fleet
  announce "cluster up"
  echo "    make observability   Prometheus + Grafana"
  echo "    make ssh             open the tunnel"
}

cmd_observability() {
  sync_project
  start_observability
  announce "observability up"
  echo "    make ssh   then open http://127.0.0.1:31495"
}

cmd_down() {
  announce "stopping the engine and Open WebUI"
  "${SSH[@]}" "$BOX" '{ pkill -u "$(id -u)" -f "[v]llm serve" || true; }; \
    docker rm -f open-webui 2>/dev/null || true; echo stopped'
  echo "The box is still running and still billing. Stop the instance in your provider's console."
}

cmd_logs() { exec "${SSH[@]}" "$BOX" "tail -f ~/$REMOTE_DIR/$ENGINE_LOG"; }

# Sync first: this runs the copy on the box, and a stale one checks nothing.
cmd_check() {
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    cluster/setup/gate.py "$BOX:~/$REMOTE_DIR/cluster/setup/gate.py"
  on_box "python3 cluster/setup/gate.py --log ~/$REMOTE_DIR/$ENGINE_LOG"
}

main() {
  local command="${1:-}"
  case "$command" in
    cluster|observability|tunnel|down|logs|check) ;;
    -h|--help|"") sed -n '2,12p' "$0"; exit 0 ;;
    *) fail "unknown command: $command   (try: up.sh --help)" ;;
  esac
  load_env
  case "$command" in
    cluster)       cmd_cluster ;;
    observability) cmd_observability ;;
    tunnel)        open_tunnel ;;
    down)          cmd_down ;;
    logs)          cmd_logs ;;
    check)         cmd_check ;;
  esac
}

main "$@"
