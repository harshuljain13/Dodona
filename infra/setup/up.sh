#!/usr/bin/env bash
# Drive the GPU box from your Mac. Normally called by the Makefile.
#
#   up.sh fleet-lambda    k3s + HAMi + KEDA → fleet → gateway → capacity check
#   up.sh fleet-runpod    the same fleet as processes — no k3s, for container boxes
#   up.sh fleet-runpod-down      stop a process fleet (fleet-runpod)
#   up.sh fleet-runpod-logs      follow the detached fleet's log
#   up.sh fleet-runpod-status    what the fleet is actually doing
#   up.sh fleet-runpod-ssh       tunnel for the process fleet's ports
#   up.sh fleet-runpod-smoke-test  prove it works, end to end
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
  echo "up.sh needs bash:  bash infra/setup/up.sh fleet-lambda   (or: make fleet-lambda)" >&2
  exit 1
fi
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # repo root, above infra/
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
  bash infra/setup/sync.sh
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

# WITH_OBSERVABILITY=1 adds prometheus and grafana. Off by default so the
# engine path does not pay for binaries it never starts.
install_packages() {
  # No announce here: box_packages.sh prints its own heading and a per-step
  # status, and two "==> packages" lines in a row read as a repeated step.
  on_box_tty "WITH_OBSERVABILITY=${WITH_OBSERVABILITY:-0} bash infra/setup/box_packages.sh"
}

# Holds the whole card, to measure the model's real footprint before slicing.
start_engine() {
  announce "engine  —  $MODEL on :8000, whole card"
  stop_engine
  on_box "source .venv/bin/activate && \
    LOCAL_MODEL='$MODEL' setsid nohup bash infra/setup/engine.sh \
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
  fail "Engine did not answer in 15 min.  bash infra/setup/up.sh logs"
}

check_capacity() {
  announce "capacity  —  checks against the running engine"
  on_box "python3 infra/lambda-cluster/setup/gate.py --log ~/$REMOTE_DIR/$ENGINE_LOG" || fail \
"    The capacity check failed, which means the arithmetic is wrong rather than
    the cluster. Read docs/project/01-capacity.md before going further."
}

start_fleet() {
  announce "fleet  —  k3s + HAMi + KEDA, then one prefill and one decode"
  # Harmless if nothing is running: the slices need every byte of the card, so
  # any whole-card engine from `up.sh engine` has to go first.
  stop_engine
  on_box_tty 'bash infra/lambda-cluster/setup/fleet.sh'
}

start_observability() {
  announce "observability  —  Prometheus + Grafana + DCGM"
  on_box_tty 'bash infra/lambda-cluster/setup/observability.sh'
}

open_tunnel() {
  announce "tunnel  —  ports forwarded to this Mac. Leave this window open."
  cat <<'PORTS'
    http://127.0.0.1:30030   Open WebUI   <- talk to it here
    http://127.0.0.1:31495   Grafana      admin / password from `make observability`
    http://127.0.0.1:8080    gateway      /metrics, /v1/chat/completions
    http://127.0.0.1:30800   vLLM prefill (30801 decode) — bypasses the gateway
PORTS
  exec bash infra/lambda-cluster/setup/tunnel.sh
}

# ── commands ───────────────────────────────────────────────────────────────

# No venv and no whole-card engine. The pods bring their own vLLM in the
# container image, and the capacity checks run against a pod inside fleet.sh
# because a whole-card measurement cannot be extrapolated to a slice anyway.
cmd_fleet_lambda() {
  require_a_host
  sync_project
  show_card
  start_fleet
  announce "cluster up"
  echo "    make observability   Prometheus + Grafana"
  echo "    make ssh             open the tunnel"
}

# One worker on the whole card, for the comparison measurement only. Needs the
# box venv, which is why it is not part of `make fleet-lambda`.
cmd_engine() {
  sync_project
  show_card
  install_packages
  start_engine
  wait_for_engine
  check_capacity
  announce "engine up"
  echo "    This holds the whole card. Run 'make fleet-lambda' to replace it with the fleet."
}

# The fleet without an orchestrator, for boxes that are containers — which is
# most rented GPUs. Deliberately does NOT call require_a_host: being a container
# is the reason this command exists.
#
# It installs vLLM, which `make fleet-lambda` does not need: there the pods bring
# their own in the image. Here the workers are processes on the box.
cmd_fleet_runpod() {
  local base="${PORT_BASE:-8100}"
  local model="${LOCAL_MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"

  # Say what is about to happen and where. Seven components land on a rented
  # box and none of them on this Mac, which is not obvious from a command run
  # on this Mac — and knowing which host owns what is the difference between
  # reading a log and guessing at one.
  announce "fleet-runpod  —  the plan"
  printf '    %-12s %s\n' "this Mac"  "ssh only. The tunnel at the end."
  printf '    %-12s %s\n' "the box"   "$BOX  —  everything below, under ~/$REMOTE_DIR"
  echo
  printf '    %-10s %-26s %s\n' "install" "python venv + vLLM"      "~/$REMOTE_DIR/.venv"
  printf '    %-10s %-26s %s\n' ""        "prometheus, grafana"     "/usr/local/bin"
  echo
  printf '    %-10s %-26s %s\n' "start"   "mooncake   :50051"       "the KV hop ledger"
  printf '    %-10s %-26s %s\n' ""        "prefill    :$base  GPU 0"  "builds the prompt KV"
  printf '    %-10s %-26s %s\n' ""        "decode-0   :$((base+1))  GPU 1"  "generates"
  printf '    %-10s %-26s %s\n' ""        "decode-1   :$((base+2))  GPU 2"  "generates"
  printf '    %-10s %-26s %s\n' ""        "gateway    :8080"        "guard · admit · place · queue"
  printf '    %-10s %-26s %s\n' ""        "prometheus :9090"        "scrapes all of the above"
  printf '    %-10s %-26s %s\n' ""        "grafana    :31495"       "the dashboards"
  echo
  printf '    %-10s %s\n' "model" "$model"
  printf '    %-10s %s\n' "logs"  "~/$REMOTE_DIR/infra/metrics/local/  —  make fleet-runpod-logs"
  echo

  sync_project
  show_card
  WITH_OBSERVABILITY=1 install_packages
  announce "fleet  —  1 vLLM per GPU, gateway, mooncake"

  # Detached, not attached. Held in the foreground this blocks the terminal, so
  # the tunnel needs a second one — and a workshop instruction that begins
  # "open another terminal" is the step everyone misses. `make fleet-runpod-down`
  # stops it, which is why detaching is safe now and was not before.
  on_box "mkdir -p infra/metrics/local && \
    setsid nohup $(workers_env) bash infra/runpod-cluster/setup/workers.sh \
      > infra/metrics/local/fleet.log 2>&1 < /dev/null & echo started"

  # Weights download on the first run and three workers load in sequence, so
  # this is minutes. Show what it is waiting for rather than a silent hang.
  local port="${PORT_BASE:-8100}"
  echo "    loading — the first run downloads the weights, so this takes a while"
  echo

  # Ask the components, do not read a log.
  #
  # This tailed fleet.log twice and printed blank lines both times — once
  # because log rotation renamed the file out from under the writer, once
  # because the fleet had not written to it yet. A progress display whose
  # failure mode is "looks identical to a hang" is worse than none.
  #
  # Each poll asks which ports answer and prints a line per change, so a
  # component appearing is visible the moment it happens.
  local probe=(ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new
               -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2
               -o BatchMode=yes)
  local waited=0 last="" state=""
  while (( waited < 2400 )); do
    state="$("${probe[@]}" "$BOX" "
      for p in 50051 9400 $base $((base+1)) $((base+2)) 8080 9090 31495; do
        curl -sf -m 2 \"localhost:\$p/metrics\" >/dev/null 2>&1 ||
        curl -sf -m 2 \"localhost:\$p/v1/models\" >/dev/null 2>&1 ||
        curl -sf -m 2 \"localhost:\$p/api/health\" >/dev/null 2>&1 && printf 'y' || printf 'n'
      done" 2>/dev/null || true)"

    # Eight characters, one per component, in start order.
    local names=(mooncake gpu prefill decode-0 decode-1 gateway prometheus grafana)
    local shown="" i=0
    for n in "${names[@]}"; do
      case "${state:$i:1}" in
        y) shown+="$n ✓  " ;;
        *) shown+="$n ·  " ;;
      esac
      i=$(( i + 1 ))
    done

    if [[ "$shown" != "$last" ]]; then
      printf '    [%2dm%02ds]  %s\n' $(( waited / 60 )) $(( waited % 60 )) "$shown"
      last="$shown"
    elif (( waited % 60 == 0 && waited > 0 )); then
      # Even with nothing new, say something each minute. A 32B takes minutes
      # per worker, and silence is indistinguishable from death.
      printf '    [%2dm%02ds]  %s\n' $(( waited / 60 )) $(( waited % 60 )) "$shown"
    fi

    # All three workers AND the gateway. Breaking on the gateway alone is how
    # `make fleet-runpod` reported success with every worker dead: the gateway
    # serves /v1/models from its own allowlist, so it answers with or without a
    # pool behind it.
    if [[ "${state:2:1}" == "y" && "${state:3:1}" == "y" \
       && "${state:4:1}" == "y" && "${state:5:1}" == "y" ]]; then
      echo; announce "fleet up"; break
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done

  # Same test as the loop, so a timeout and a success cannot disagree about
  # what "up" means.
  if [[ "${state:2:1}" != "y" || "${state:3:1}" != "y" \
     || "${state:4:1}" != "y" || "${state:5:1}" != "y" ]]; then
    fail "  the fleet did not come up — the last line above shows which part.
  Logs:  make fleet-runpod-logs
  Stop:  make fleet-runpod-down"
  fi

  echo "    gateway   127.0.0.1:8080/v1"
  echo "    workers   $port  $((port+1))  $((port+2))"
  echo "    logs      make fleet-runpod-logs          stop   make fleet-runpod-down"
  echo

  # And the tunnel, in the foreground, which is the thing to leave open.
  cmd_fleet_runpod_ssh
}

# Proves the fleet works rather than that it started. Syncs first, like every
# other remote command.
cmd_fleet_smoke() {
  sync_project >/dev/null 2>&1 || true
  on_box "$(workers_env) GRAFANA_PASSWORD=$(printf %q "${GRAFANA_PASSWORD:-admin}") bash infra/runpod-cluster/setup/smoke.sh"
}

# The process fleet's own tunnel. `up.sh tunnel` forwards the Kubernetes
# NodePorts — 30030 Open WebUI, 30800/30801 the fleet Services — none of which
# exist here, so it advertised addresses that could never answer. This forwards
# exactly what fleet-runpod starts, and says what each one is.
cmd_fleet_runpod_ssh() {
  local base="${PORT_BASE:-8100}"
  announce "tunnel  —  the process fleet. Leave this open."
  echo "    gateway      http://127.0.0.1:8080/v1"
  echo "    grafana      http://127.0.0.1:31495      admin / ${GRAFANA_PASSWORD:-admin}"
  echo "    prometheus   http://127.0.0.1:9090"
  echo "    workers      $base (prefill)  $((base+1)) $((base+2)) (decode)"
  echo "    mooncake     http://127.0.0.1:50051/metrics"
  echo
  exec ssh -i "$SSH_KEY" -p "$SSH_PORT" \
    -o StrictHostKeyChecking=accept-new \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
    -L 8080:127.0.0.1:8080 \
    -L "$base:127.0.0.1:$base" \
    -L "$((base+1)):127.0.0.1:$((base+1))" \
    -L "$((base+2)):127.0.0.1:$((base+2))" \
    -L 9090:127.0.0.1:9090 \
    -L 31495:127.0.0.1:31495 \
    -L 50051:127.0.0.1:50051 \
    -t "$BOX" "mkdir -p ~/$REMOTE_DIR; cd ~/$REMOTE_DIR; exec bash -l"
}

# Asks the fleet what it is doing rather than reading ports. Syncs first: the
# box only has what the last sync left, and a status script that is not there
# reports a broken repo instead of a fleet.
cmd_fleet_status() {
  sync_project >/dev/null 2>&1 || true
  on_box "$(workers_env) GRAFANA_PASSWORD=$(printf %q "${GRAFANA_PASSWORD:-admin}") bash infra/runpod-cluster/setup/status.sh"
}

# The fleet runs detached, so its output is a file rather than your terminal.
cmd_logs_fleet() {
  load_env >/dev/null 2>&1 || true
  on_box "tail -n 80 -f infra/metrics/local/fleet.log"
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

# No equivalent on the Kubernetes path: there `make clean` deletes the
# workloads and k3s goes with them. Raw processes have no such owner.
cmd_fleet_down() {
  # PORT_BASE, so the port report at the end names the right ones.
  # Sync first. down.sh is a file on the box, and the box only has whatever the
  # last sync left there — so a newly added script is "No such file or
  # directory", which reads as a broken repo rather than a stale one. Every
  # other command that runs a remote script syncs before it; this one did not.
  sync_project
  announce "fleet-down  —  stopping the process fleet"
  on_box "$(workers_env) bash infra/runpod-cluster/setup/down.sh"
}

cmd_observability() {
  require_a_host
  sync_project
  start_observability
  announce "observability up"
  echo "    make ssh   then open http://127.0.0.1:31495"
}

# Only the whole-card engine. The pods are k3s's and `make fleet-lambda` owns them;
# `docker` on this box needs sudo anyway.
cmd_down() {
  announce "stopping the whole-card engine"
  "${SSH[@]}" "$BOX" '{ pkill -u "$(id -u)" -f "[v]llm serve" || true; }; echo stopped'
  echo "    Pods keep running:  sudo k3s kubectl delete -f infra/lambda-cluster/k8s/fleet/"
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
  on_box_tty 'bash infra/lambda-cluster/setup/clean.sh'
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
    infra/lambda-cluster/setup/gate.py "$BOX:~/$REMOTE_DIR/infra/lambda-cluster/setup/gate.py"
  on_box "python3 infra/lambda-cluster/setup/gate.py --log ~/$REMOTE_DIR/$ENGINE_LOG"
}

main() {
  local command="${1:-}"
  case "$command" in
    fleet-lambda|fleet-runpod|fleet-runpod-down|fleet-runpod-logs|fleet-runpod-status|fleet-runpod-ssh|fleet-runpod-smoke-test|observability|tunnel|engine|down|logs|check|clean|load|kv) ;;
    # Print the header block by shape, not by line number: `sed -n '2,14p'`
    # dropped `clean` off the end the moment two commands were added, and the
    # test that catches it is the only reason anyone noticed.
    -h|--help|"") sed -n '2,/^$/p' "$0" | sed '$d'; exit 0 ;;
    *) fail "unknown command: $command   (try: up.sh --help)" ;;
  esac
  load_env
  case "$command" in
    fleet-lambda)  cmd_fleet_lambda ;;
    fleet-runpod)  cmd_fleet_runpod ;;
    fleet-runpod-down)    cmd_fleet_down ;;
    fleet-runpod-logs)    cmd_logs_fleet ;;
    fleet-runpod-status)  cmd_fleet_status ;;
    fleet-runpod-ssh)     cmd_fleet_runpod_ssh ;;
    fleet-runpod-smoke-test) cmd_fleet_smoke ;;
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
